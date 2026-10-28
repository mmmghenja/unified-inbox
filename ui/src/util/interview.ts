// Ф60-1C: подписи и форматы панели помощника на интервью.
import type { Interview } from '../api/types';

export const LISTEN_ENABLE_COMMAND = 'systemctl --user enable --now hub-listen.service';

export const INTERVIEW_END_CONFIRM = 'Закончить интервью? Запись остановится, сессия получит «звонок окончен» и напишет итог.';

export function modeLabel(mode: Interview['mode']): string {
  return mode === 'live' ? 'по ходу' : 'по кнопке';
}

/** Сколько минут идёт интервью (не меньше 0). */
export function interviewMinutes(started: number, now: number): number {
  return Math.max(0, Math.floor((now - started) / 60000));
}

/** Время строки `ЧЧ:ММ:СС` по местным часам. */
export function clock(ms: number): string {
  const d = new Date(ms);
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function whoLabel(who: 'me' | 'them'): string {
  return who === 'me' ? 'Я' : 'Собеседник';
}
