// Ф12-2 C (`docs/architecture.md`): подписи черновиков по графику — «уйдёт в ЧЧ:ММ» / «завтра в
// ЧЧ:ММ» и итог подтверждения пачкой. Чистые функции без Vue; время — местное время браузера (оно же у владельца).
import { ApiError } from '../api/client';
import type { ApproveBatchResult } from '../api/types';
import { formatTime, startOfDay } from './home';

const DAY_MS = 24 * 60 * 60 * 1000;

/** Слот относительно сегодня: `ЧЧ:ММ` (сегодня), `завтра ЧЧ:ММ`, иначе `ДД.ММ ЧЧ:ММ`. */
function slotMoment(ts: number, nowMs: number): { day: 'today' | 'tomorrow' | 'other'; text: string } {
  const today = startOfDay(nowMs);
  const day = startOfDay(ts);
  const time = formatTime(ts);
  if (day === today) return { day: 'today', text: time };
  // Сравнение по началу суток следующего дня: переход часов внутри суток не сдвигает «завтра».
  if (day === startOfDay(today + DAY_MS + DAY_MS / 2)) return { day: 'tomorrow', text: time };
  const date = new Date(ts).toLocaleDateString([], { day: '2-digit', month: '2-digit' });
  return { day: 'other', text: `${date} в ${time}` };
}

/** Пометка запланированного черновика: «уйдёт в 14:30», «завтра в 10:07», «уйдёт 30.09 в 10:07». */
export function slotLabel(ts: number, nowMs: number = Date.now()): string {
  const moment = slotMoment(ts, nowMs);
  if (moment.day === 'today') return `уйдёт в ${moment.text}`;
  if (moment.day === 'tomorrow') return `завтра в ${moment.text}`;
  return `уйдёт ${moment.text}`;
}

function shortSlot(ts: number, nowMs: number): string {
  const moment = slotMoment(ts, nowMs);
  if (moment.day === 'tomorrow') return `завтра ${moment.text}`;
  return moment.text;
}

/** Итог подтверждения пачкой: «подтверждено N, запланировано на ЧЧ:ММ–ЧЧ:ММ» (без слотов — «подтверждено N»). */
export function batchSummary(results: ApproveBatchResult[], nowMs: number = Date.now()): string {
  const ok = results.filter((r) => r.status === 'queued');
  const slots = ok
    .map((r) => r.not_before_ts)
    .filter((ts): ts is number => typeof ts === 'number')
    .sort((a, b) => a - b);
  let line = `подтверждено ${ok.length}`;
  if (slots.length > 0) {
    const first = shortSlot(slots[0], nowMs);
    const last = shortSlot(slots[slots.length - 1], nowMs);
    line += `, запланировано на ${first === last ? first : `${first}–${last}`}`;
  }
  return line;
}

/** Ошибки пачки по пунктам: «#12: invalid transition»; Ф30-1 — «#12: время отправки 10:00 прошло». */
export function batchErrors(results: ApproveBatchResult[]): string[] {
  return results
    .filter((r) => r.status === 'error')
    .map((r) => `#${r.id}: ${r.error === SEND_AT_PASSED ? (r.message ?? 'время отправки прошло') : (r.error ?? 'ошибка')}`);
}

// Ф30-1 C (`docs/architecture.md`): черновик сессии «к точному времени» (`send_at_ts`).
export const SEND_AT_PASSED = 'send_at_passed';
export const SEND_AT_LATE = 'send_at_late';
export const SEND_AT_LATE_TEXT = 'опоздало: хаб не работал в назначенное время';

const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

/** Пометка черновика с `send_at`: «к 10:00, 5 окт». */
export function sendAtLabel(ts: number): string {
  const day = new Date(ts);
  return `к ${formatTime(ts)}, ${day.getDate()} ${MONTHS[day.getMonth()]}`;
}

// Ф30-2 (`docs/architecture.md`: догон к сроку с `cancel_if_reply` не
// уйдёт, если после подтверждения в беседе ответят; неудача — текст ядра как есть (`outboxErrorText`).
export const CANCEL_IF_REPLY_TEXT = 'не уйдёт, если ответят';

/** Ф30-2: добавка к пометке времени строки с `send_at` и флажком — « · не уйдёт, если ответят», иначе ''. */
export function cancelIfReplyMark(sendAtTs: number | null | undefined, cancelIfReply: boolean | undefined): string {
  return typeof sendAtTs === 'number' && cancelIfReply === true ? ` · ${CANCEL_IF_REPLY_TEXT}` : '';
}

/** Ответ 409 `send_at_passed` на «Отправить»: время отправки прошло — владелец может «Отправить сейчас». */
export function isSendAtPassed(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 409) return false;
  const body = err.body as { error?: unknown } | null;
  return body !== null && typeof body === 'object' && body.error === SEND_AT_PASSED;
}

/** Id пачки, у которых время отправки прошло (для кнопок «Отправить сейчас»). */
export function batchPassed(results: ApproveBatchResult[]): number[] {
  return results.filter((r) => r.status === 'error' && r.error === SEND_AT_PASSED).map((r) => r.id);
}

/** Причина `failed` простыми словами: опоздание `send_at` — своим текстом, прочее — как есть. */
export function outboxErrorText(error: string | null | undefined): string | null {
  if (!error) return null;
  return error === SEND_AT_LATE ? SEND_AT_LATE_TEXT : error;
}
