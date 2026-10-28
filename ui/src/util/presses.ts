// Ф44-1d: нажатия кнопок ботов — выбор карточек «Нажать?», состояния кнопок и строки исхода под клавиатурой.
// Чистые функции без Vue. `text` кнопки, `role`, `channel_title`, `quote` и ответ бота (`result`) — чужой ввод:
// сюда они попадают строками для текстовой интерполяции.
import type { ButtonPress } from '../api/types';

/** Исход старше суток под клавиатурой не показывается (ядро и так отдаёт за сутки). */
export const PRESS_OUTCOME_TTL_MS = 24 * 60 * 60 * 1000;
/** Сколько строк исхода показывать под одним сообщением. */
export const PRESS_LINES_MAX = 3;

export function isLive(press: ButtonPress): boolean {
  return press.status === 'pending' || press.status === 'approved' || press.status === 'leased';
}

/** Карточки «Нажать?» — только ждущие решения владельца, по порядку подачи. */
export function visiblePresses(presses: ButtonPress[]): ButtonPress[] {
  return presses.filter((p) => p.status === 'pending').sort((a, b) => a.id - b.id);
}

/** Нажатия одного сообщения: по кнопке — последнее (с наибольшим id). */
export function latestByButton(presses: ButtonPress[], messageId: number): ButtonPress[] {
  const latest = new Map<string, ButtonPress>();
  for (const p of presses) {
    if (p.message_id !== messageId) continue;
    const key = `${p.row}.${p.col}`;
    const prev = latest.get(key);
    if (prev === undefined || prev.id < p.id) latest.set(key, p);
  }
  return [...latest.values()].sort((a, b) => a.id - b.id);
}

/** Ссылка из ответа бота — только http/https; иначе `null` (показать текстом, не ссылкой). */
export function safeHttpUrl(raw: string | undefined): URL | null {
  if (typeof raw !== 'string') return null;
  try {
    const url = new URL(raw);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

const BOT_GUARD_WORD: Record<string, string> = {
  payment: 'оплата',
  secret: 'секрет',
  delete: 'удаление',
  pdn: 'личные данные',
  file: 'файл',
};

/** Ф63-2: категория страховки из `bot_guard` черновика или `reason == "bot_guard:<кат>"` нажатия; иначе `null`. */
export function botGuardCategory(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const cat = value.startsWith('bot_guard:') ? value.slice('bot_guard:'.length) : value;
  return cat in BOT_GUARD_WORD ? cat : null;
}

/** Пометка страховки; `null` — нет. Для нажатия — `press = true` и `reason` (нужен префикс `bot_guard:`). */
export function botGuardNote(value: string | null | undefined, press = false): string | null {
  if (press && !(typeof value === 'string' && value.startsWith('bot_guard:'))) return null;
  const cat = botGuardCategory(value);
  return cat === null ? null : `страховка: ${BOT_GUARD_WORD[cat]} — проверь перед отправкой`;
}

const FAIL_REASON: Record<string, string> = {
  stale_button: 'клавиатура изменилась — этой кнопки уже нет',
  flood: 'Telegram просит подождать — не нажато',
  not_sent: 'не ушло: нет связи с Telegram',
  bad_ref: 'сообщение не найдено в Telegram',
  unsupported: 'такое нажатие не поддерживается',
};

const UNKNOWN_TEXT = 'исход неизвестен — проверь беседу, повторно вслепую не нажимай';

export type OutcomeKind = 'live' | 'ok' | 'alert' | 'warn';

export interface OutcomeLine {
  key: string;
  kind: OutcomeKind;
  /** Строка целиком: «<кнопка>: …». */
  text: string;
  /** Ссылка из ответа бота (только http/https) — рисуется ссылкой, сама не открывается. */
  url: string | null;
  /** Домен ссылки — подпись (полный адрес в подсказке). */
  host?: string | null;
}

function failText(press: ButtonPress): string {
  const reason = press.reason ?? '';
  return FAIL_REASON[reason] ?? (reason ? `не удалось: ${reason}` : 'не удалось');
}

function expiredText(press: ButtonPress): string {
  if (press.reason === 'not_sent_in_time') return 'не ушло в срок';
  if (press.reason === 'not_decided') return 'не подтверждено за сутки';
  return 'не ушло в срок';
}

/** Строка исхода одного нажатия; `null` — нечего показывать (давно закрытое). */
export function outcomeLine(press: ButtonPress, nowMs: number): OutcomeLine | null {
  const head = `«${press.text}»`;
  const key = `press-${press.id}`;
  switch (press.status) {
    case 'pending':
      return { key, kind: 'live', text: `${head}: ждёт подтверждения владельца`, url: null };
    case 'approved':
    case 'leased':
      return { key, kind: 'live', text: `${head}: нажимается…`, url: null };
    default:
      break;
  }
  if (nowMs - press.updated_ts > PRESS_OUTCOME_TTL_MS) return null;
  if (press.status === 'declined') return { key, kind: 'warn', text: `${head}: отклонено`, url: null };
  if (press.status === 'expired') return { key, kind: 'warn', text: `${head}: ${expiredText(press)}`, url: null };
  if (press.status === 'unknown') return { key, kind: 'warn', text: `${head}: ${UNKNOWN_TEXT}`, url: null };
  if (press.status === 'failed') return { key, kind: 'warn', text: `${head}: ${failText(press)}`, url: null };
  // done
  const result = press.result ?? null;
  const message = result?.message?.trim();
  const parsed = safeHttpUrl(result?.url);
  const url = parsed?.href ?? null;
  const host = parsed?.hostname ?? null;
  if (message) {
    return { key, kind: result?.alert ? 'alert' : 'ok', text: `${head}: бот ответил — ${message}`, url, host };
  }
  if (url !== null) return { key, kind: 'ok', text: `${head}: бот просит открыть ссылку`, url, host };
  if (result?.timeout) {
    return { key, kind: 'ok', text: `${head}: нажато, бот не ответил на запрос — смотри новые сообщения`, url: null };
  }
  return { key, kind: 'ok', text: `${head}: нажато`, url: null };
}

/** Строки под клавиатурой сообщения: по кнопке последнее нажатие, новые последними, не больше `PRESS_LINES_MAX`. */
export function outcomeLines(presses: ButtonPress[], messageId: number, nowMs: number): OutcomeLine[] {
  const lines: OutcomeLine[] = [];
  for (const p of latestByButton(presses, messageId)) {
    const line = outcomeLine(p, nowMs);
    if (line !== null) lines.push(line);
  }
  return lines.slice(-PRESS_LINES_MAX);
}

/** Адреса кнопок сообщения (`row.col`) с живой строкой: `pending` — ждёт владельца; `approved`/`leased` — нажимается. */
export function liveButtons(presses: ButtonPress[], messageId: number): { waiting: string[]; busy: string[] } {
  const waiting: string[] = [];
  const busy: string[] = [];
  for (const p of latestByButton(presses, messageId)) {
    if (p.status === 'pending') waiting.push(`${p.row}.${p.col}`);
    else if (p.status === 'approved' || p.status === 'leased') busy.push(`${p.row}.${p.col}`);
  }
  return { waiting, busy };
}

/** Заголовок карточки «Нажать?». */
export function pressHeadline(press: ButtonPress): string {
  const role = press.role?.trim();
  const who = role ? `просит сессия «${role}»` : 'просит сессия';
  const where = press.channel_title?.trim() ? ` · ${press.channel_title.trim()}` : ` · беседа #${press.channel_id}`;
  return `Нажать «${press.text}»?${where} · ${who}`;
}
