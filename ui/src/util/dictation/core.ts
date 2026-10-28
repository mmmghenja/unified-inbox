// Ф59-1: ядро диктовки без DOM и без Web Audio — очередь кусков и вставка текста в поле. Куски уходят в ядро хаба
// СТРОГО ПО ОДНОМУ, текст склеивается в порядке записи, а не в порядке ответов сети. Звук сюда приходит уже
// готовым (16 кГц Int16LE, см. `capture.ts`); сеть подменяется в тестах (`send`).
// 19.10: временный сбой (сеть, таймаут, 5xx, 429 частоты) больше не теряет кусок — он повторяется с паузой
// 1-2-4-8-15-30 с (дальше раз в 30 с), пока не дойдёт или поле не закроют; следующие куски ждут за ним, порядок
// текста не ломается.

export type DictateErrorKind =
  /** Временный сбой (429, 5xx, сеть, таймаут) — кусок держится в очереди и повторяется. */
  | 'retry'
  /** Ядро ответило 404 `dictate_off`: ключ не задан (или убрали посреди записи). */
  | 'off'
  /** Суточный лимит (429 `dictate_daily`). */
  | 'daily'
  /** Ключ SpeechKit не принят (502 `dictate_key`). */
  | 'key';

export const DICTATE_ERROR_TEXT: Record<DictateErrorKind | 'mic' | 'unsupported', string> = {
  retry: 'нет связи — повторяю, запись не пропадёт',
  off: 'диктовка не настроена',
  daily: 'лимит диктовки на сегодня',
  key: 'ключ диктовки не принят',
  mic: 'нет доступа к микрофону',
  unsupported: 'диктовка в этом браузере недоступна',
};

/** Ошибка, как её видит ядро: статус и `detail` (`ApiError.message`) — без привязки к клиенту API. */
export interface DictateFailure {
  status: number;
  detail: string;
}

/** Куда класть текст: статус `null` — сеть/таймаут. `bad_audio` и прочие 4xx — брак нашего куска, молча пропускаем. */
export function classifyFailure(err: DictateFailure | null): DictateErrorKind | 'skip' {
  if (err === null) return 'retry';
  if (err.status === 404) return 'off';
  // Сессия интерфейса истекла или устарела — после входа в другой вкладке кусок дойдёт.
  if (err.status === 401 || err.status === 403) return 'retry';
  if (err.status === 429) return err.detail === 'dictate_daily' ? 'daily' : 'retry';
  if (err.status === 502 && err.detail === 'dictate_key') return 'key';
  if (err.status === 502 || err.status === 504 || err.status >= 500) return 'retry';
  return 'skip';
}

/** 21.10: кусок речи после конца предложения (или в пустое поле) — с заглавной: whisper начинает каждый кусок
 * как придётся, а куски режутся по паузам посреди фраз. */
export function sentenceStart(before: string, text: string): string {
  if (text === '' || !/^[a-zа-яё]/.test(text)) return text;
  const tail = before.trimEnd();
  if (tail !== '' && !/[.!?…]$/.test(tail)) return text;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Вставить фрагмент в позицию курсора поля. Склейка пробелами: слева — если там не пробел, справа — если там
 * не пробел. Поле получает `input` (всплывающее) — `v-model` подхватывает. Возвращает вставленную строку.
 */
export function insertAtCursor(el: HTMLTextAreaElement | HTMLInputElement, fragment: string): string {
  const value = el.value;
  const start = el.selectionStart ?? value.length;
  const end = el.selectionEnd ?? start;
  const before = value.slice(0, start);
  const text = sentenceStart(before, fragment.trim());
  if (text === '') return '';
  const after = value.slice(end);
  const left = before !== '' && !/\s$/.test(before) ? ' ' : '';
  const right = after !== '' && !/^\s/.test(after) ? ' ' : '';
  const inserted = left + text + right;
  el.setRangeText(inserted, start, end, 'end');
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return inserted;
}

export interface DictationOptions {
  /** Один запрос куска. Бросает `DictateFailure`-подобное (`{status, detail}`) или любое другое (сеть). */
  send: (bytes: Uint8Array, signal: AbortSignal) => Promise<{ text: string }>;
  /** Непустой текст пришёл — вставить в поле. */
  insert: (text: string) => void;
  /** Таймаут одного запроса, мс. */
  timeoutMs?: number;
  /** Паузы перед повторами временного сбоя, мс; дальше последнего — последнее. */
  retryDelaysMs?: number[];
}

export interface DictationCallbacks {
  onPendingChange: (pending: boolean) => void;
  onError: (kind: DictateErrorKind) => void;
  /** Диктовка больше не работает в этой сессии (`off`, `daily`, `key`): очередь сброшена. */
  onDisabled: (kind: DictateErrorKind) => void;
  /** Кусок дошёл после временного сбоя — сообщение «нет связи» можно убрать. */
  onRecovered?: () => void;
}

const DEFAULT_TIMEOUT_MS = 20_000;
const DEFAULT_RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 30000];

function asFailure(e: unknown): DictateFailure | null {
  if (typeof e === 'object' && e !== null && 'status' in e && typeof (e as { status: unknown }).status === 'number') {
    const err = e as { status: number; message?: unknown };
    return { status: err.status, detail: typeof err.message === 'string' ? err.message : '' };
  }
  return null;
}

export function createDictation(options: DictationOptions, cb: DictationCallbacks) {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const retryDelays = options.retryDelaysMs?.length ? options.retryDelaysMs : DEFAULT_RETRY_DELAYS_MS;
  let disabled = false;
  let disposed = false;
  let pendingCount = 0;
  let currentAbort: AbortController | null = null;
  let wakeWait: (() => void) | null = null;
  let chain: Promise<void> = Promise.resolve();

  function setPending(delta: number): void {
    pendingCount += delta;
    cb.onPendingChange(pendingCount > 0);
  }

  /** Пауза перед повтором; `dispose()` её обрывает. */
  function wait(ms: number): Promise<void> {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        wakeWait = null;
        resolve();
      }, ms);
      wakeWait = () => {
        clearTimeout(timer);
        wakeWait = null;
        resolve();
      };
    });
  }

  /** Одна попытка: `ok` — дошёл (или пустой текст), `skip` — брак куска, `retry` — временный сбой, `stop` — конец. */
  async function tryOnce(bytes: Uint8Array): Promise<'ok' | 'skip' | 'retry' | 'stop'> {
    const controller = new AbortController();
    currentAbort = controller;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    try {
      const data = await options.send(bytes, controller.signal);
      if (disposed) return 'stop';
      if (typeof data.text === 'string' && data.text.trim() !== '') options.insert(data.text);
      return 'ok';
    } catch (e) {
      if (disposed) return 'stop';
      // Отмена без нашего таймаута — это dispose(), тихо.
      if (e instanceof Error && e.name === 'AbortError' && !timedOut) return 'stop';
      const kind = classifyFailure(timedOut ? null : asFailure(e));
      if (kind === 'skip' || kind === 'retry') return kind;
      disabled = true;
      cb.onDisabled(kind);
      return 'stop';
    } finally {
      clearTimeout(timer);
      currentAbort = null;
    }
  }

  async function sendOne(bytes: Uint8Array): Promise<void> {
    if (disposed || disabled || bytes.length === 0) return;
    setPending(1);
    try {
      for (let attempt = 0; ; attempt++) {
        const outcome = await tryOnce(bytes);
        if (outcome !== 'retry') {
          if (outcome === 'ok' && attempt > 0) cb.onRecovered?.();
          return;
        }
        if (attempt === 0) cb.onError('retry');
        await wait(retryDelays[Math.min(attempt, retryDelays.length - 1)]);
        if (disposed || disabled) return;
      }
    } finally {
      setPending(-1);
    }
  }

  function enqueue(bytes: Uint8Array): Promise<void> {
    const next = chain.then(() => sendOne(bytes));
    chain = next;
    return next;
  }

  return {
    /** Кусок из нарезчика — во время записи. */
    pushChunk(bytes: Uint8Array): void {
      if (disabled || disposed || bytes.length === 0) return;
      void enqueue(bytes);
    },
    /** «Стоп»: отдать хвост и дождаться очереди целиком. */
    stopAndFlush(tail?: Uint8Array): Promise<void> {
      if (tail && tail.length > 0 && !disabled && !disposed) return enqueue(tail);
      return chain;
    },
    dispose(): void {
      disposed = true;
      currentAbort?.abort();
      wakeWait?.();
    },
    isDisabled: () => disabled,
    isPending: () => pendingCount > 0,
  };
}

export type Dictation = ReturnType<typeof createDictation>;
