// Подключение `EventSource('/api/v1/stream')`. Спека + решение архитектора по heartbeat (20.09):
// - `event: hb` (без `id:`, `lastEventId` не двигает) — ТОЛЬКО обновляет давность, `/sync` не вызывается.
// - `event: message` / `event: channel` — обновляет давность И запускает троттлинг-`/sync` (~50 мс).
// - `onopen` — тоже обновляет давность (переподключение само по себе не значит «есть новости», но
//   означает «поток жив снова»).
//
// F3 (приёмка 6b, чтение кода архитектором): раньше `scheduleSync` был ДЕБАУНСОМ — каждый новый кадр
// откладывал таймер заново, и при непрерывном потоке (flood) `/sync` не вызывался НИКОГДА. Теперь это
// ТРОТТЛИНГ: если таймер уже взведён — не трогаем его, он всё равно сработает.
//
// F2 (приёмка 6b): по спецификации EventSource при фатальном HTTP-ответе (не 200, например 401 после
// истечения сессии, 5xx/421 при рестарте core) агент «проваливает соединение»: `readyState` становится
// `CLOSED` (2), событие `error` приходит, но браузер САМ больше не переподключается (это не то же самое, что
// временный обрыв сети, при котором `readyState` остаётся `CONNECTING` (0) и реконнект штатный). Ловим
// именно фатальный случай и отдаём наружу `onClosed()` — переподключение и восполнение дыры через `/sync`
// (Last-Event-ID у нового EventSource нет) делает вызывающая сторона (`store.ts`), т.к. ей нужно сперва
// проверить сессию (`authMe()`), а не слепо реконнектиться.
import { recordFrame, type StalenessState } from './staleness';

export interface EventSourceLike {
  readonly readyState: number;
  addEventListener(type: string, listener: () => void): void;
  close(): void;
}

/** Значения `EventSource.readyState` — дублируем константы браузера, чтобы не тянуть DOM-тип в фейки тестов. */
export const EVENT_SOURCE_CLOSED = 2;

export interface ConnectStreamOptions {
  url: string;
  staleness: StalenessState;
  onSyncNeeded: () => void;
  /** Фатальное закрытие потока (readyState CLOSED после error) — реконнект решает вызывающая сторона. */
  onClosed?: () => void;
  /** Поток открылся (включая переоткрытие) — сигнал сбросить счётчик бэкоффа реконнекта. */
  onOpen?: () => void;
  throttleMs?: number;
  /** Инъекция для тестов (jsdom не реализует EventSource) — по умолчанию настоящий браузерный EventSource. */
  createEventSource?: (url: string) => EventSourceLike;
}

export interface StreamHandle {
  close(): void;
}

function defaultCreateEventSource(url: string): EventSourceLike {
  return new EventSource(url) as unknown as EventSourceLike;
}

export function connectStream(opts: ConnectStreamOptions): StreamHandle {
  const create = opts.createEventSource ?? defaultCreateEventSource;
  const throttleMs = opts.throttleMs ?? 50;
  const es = create(opts.url);

  let timer: ReturnType<typeof setTimeout> | null = null;
  const scheduleSync = (): void => {
    if (timer !== null) return; // троттлинг: таймер уже взведён — новый кадр его не откладывает (F3)
    timer = setTimeout(() => {
      timer = null;
      opts.onSyncNeeded();
    }, throttleMs);
  };

  es.addEventListener('open', () => {
    recordFrame(opts.staleness);
    opts.onOpen?.();
  });
  es.addEventListener('hb', () => recordFrame(opts.staleness));
  es.addEventListener('message', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  es.addEventListener('channel', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф41-2б-2: кадры постоянных сессий хаба (`hub_session`, `session_request`) — только метаданные, запись и запрос
  // целиком приходят `/sync` (`hub_sessions`/`session_requests`).
  es.addEventListener('hub_session', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  es.addEventListener('session_request', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф17-1b: кадр заявки на вступление несёт только `id/project_id/status/seq` — заявка целиком приходит `/sync`.
  es.addEventListener('join', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф38-2: кадр заявки на правило раскладки (`id/project_id/status/seq`) — как `join`, заявка целиком приходит `/sync`.
  es.addEventListener('agent_request', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  es.addEventListener('rule_request', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф44-1d: кадр нажатия кнопки бота (`id/channel_id/message_id/project_id/status/seq`) — как `join`, строка целиком
  // (с ответом бота) приходит `/sync`.
  es.addEventListener('press', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф54-1: кадр заявки «нужен человек» (`id/project_id/kind/status/seq`, без текстов автора) — `/sync` перечитывается.
  es.addEventListener('human_request', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф17-2b: кадр заявки на выход (`id/project_id/status/seq`) — как `join`, заявка целиком приходит `/sync`.
  es.addEventListener('leave', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф62-6: кадр заявки на группу WhatsApp (`id/kind/project_id/channel_id/status/seq`, без названия и участников) —
  // заявка целиком приходит `/sync`.
  es.addEventListener('group_op', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф34-2: кадр напоминания (`id/project_id/channel_id/status/seq`) — как `join`, строка целиком приходит `/sync`.
  es.addEventListener('reminder', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  // Ф35-3: кадр сообщения агентов (`id/status/seq`) — как `reminder`, строка целиком приходит `/sync`.
  es.addEventListener('peer_msg', () => {
    recordFrame(opts.staleness);
    scheduleSync();
  });
  es.addEventListener('error', () => {
    if (es.readyState === EVENT_SOURCE_CLOSED) {
      opts.onClosed?.();
    }
  });

  return {
    close(): void {
      if (timer !== null) clearTimeout(timer);
      es.close();
    },
  };
}
