// Ф34-2 (`docs/architecture.md`: напоминания по беседам — чистые функции без Vue.
// Время показывается в локальном поясе браузера; «до завтра 10:00» — локальные 10:00, на сервер уходит ISO со
// смещением. Заметка и название беседы — чужие данные: здесь только строки, в шаблонах — текстовая интерполяция.
import type { Reminder } from '../api/types';

export const TOMORROW_HOUR = 10;
export const HOUR_MS = 60 * 60 * 1000;

/** Сработавшие (ждут реакции владельца): по сроку, потом по номеру. */
export function firedReminders(items: Iterable<Reminder>): Reminder[] {
  return [...items].filter((r) => r.status === 'fired').sort((a, b) => a.due_ts - b.due_ts || a.id - b.id);
}

/** Ждущие срока напоминания беседы — ближайшие первыми. */
export function pendingOfChannel(items: Iterable<Reminder>, channelId: number): Reminder[] {
  return [...items]
    .filter((r) => r.channel_id === channelId && (r.status === 'pending' || (r.status === 'fired' && isRecurring(r))))
    .sort((a, b) => a.due_ts - b.due_ts || a.id - b.id);
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function clock(d: Date): string {
  return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

/** «14:30» (сегодня), «завтра 10:00», иначе «5 окт 14:30» — в локальном поясе. */
export function whenLabel(ts: number, nowMs: number = Date.now()): string {
  const d = new Date(ts);
  const now = new Date(nowMs);
  if (sameDay(d, now)) return clock(d);
  const tomorrow = new Date(nowMs);
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (sameDay(d, tomorrow)) return `завтра ${clock(d)}`;
  return `${d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })} ${clock(d)}`;
}

/** Подпись значка в шапке: «⏰ 14:30» или «⏰ 3» (несколько). */
export function badgeLabel(pending: Reminder[], nowMs: number = Date.now()): string {
  if (pending.length === 0) return '';
  if (pending.length === 1) return `⏰ ${whenLabel(pending[0].due_ts, nowMs)}`;
  return `⏰ ${pending.length}`;
}

/** Кто поставил: «я», роль сессии или «сессия». */
export function whoLabel(r: Reminder): string {
  if (r.by === 'me') return 'я';
  return r.role !== null && r.role !== '' ? r.role : 'сессия';
}

/** Через час от `nowMs`. */
export function inAnHour(nowMs: number = Date.now()): number {
  return nowMs + HOUR_MS;
}

/** Завтра в 10:00 по локальным часам (`nowMs` — сейчас). */
export function tomorrowTen(nowMs: number = Date.now()): number {
  const d = new Date(nowMs);
  d.setDate(d.getDate() + 1);
  d.setHours(TOMORROW_HOUR, 0, 0, 0);
  return d.getTime();
}

/** ISO со смещением (`...Z`) — формат, который принимает ядро. */
export function isoOf(ts: number): string {
  return new Date(ts).toISOString();
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** Значение для `<input type="datetime-local">` (локальное, без секунд). */
export function toLocalInput(ts: number): string {
  const d = new Date(ts);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Из значения `datetime-local` — миллисекунды; пусто или мусор — `null`. */
export function fromLocalInput(value: string): number | null {
  if (value.trim() === '') return null;
  const ts = new Date(value).getTime();
  return Number.isNaN(ts) ? null : ts;
}

/** Ф89-1: повторяемое ли напоминание. */
export function isRecurring(r: Reminder): boolean {
  return r.every !== undefined && r.every !== null && r.every !== '';
}

/** Ф89-1: периоды для постановки. `every` — значение для ядра; `null` — разовое. */
export const EVERY_CHOICES: { value: string; label: string }[] = [
  { value: '', label: 'разово' },
  { value: 'day', label: 'каждый день' },
  { value: 'weekdays', label: 'по будням' },
  { value: 'week', label: 'раз в неделю' },
  { value: 'hours', label: 'каждые N ч' },
];

/** `every` для запроса из выбора и числа часов; кривое число часов — `undefined` (форма покажет ошибку). */
export function everyOf(choice: string, hours: string): string | null | undefined {
  if (choice === '') return null;
  if (choice !== 'hours') return choice;
  const n = Number.parseInt(hours, 10);
  return Number.isInteger(n) && n >= 1 && String(n) === hours.trim() ? `${n}h` : undefined;
}

/** «последний раз: 14:30, маркетолог» / «…, владелец». */
export function lastDoneLabel(r: Reminder, nowMs: number = Date.now()): string | null {
  if (r.last_done_ts === undefined || r.last_done_ts === null) return null;
  const who = r.last_done_role !== undefined && r.last_done_role !== null && r.last_done_role !== '' ? r.last_done_role : 'Владелец';
  return `последний раз: ${whenLabel(r.last_done_ts, nowMs)}, ${who}`;
}
