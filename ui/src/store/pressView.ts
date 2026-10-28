// Ф44-1d: что показать у клавиатуры сообщения — состояния кнопок и строки исхода (по стору нажатий).
import type { ButtonPress } from '../api/types';
import { store } from './instance';
import { liveButtons, outcomeLines, type OutcomeLine } from '../util/presses';

export interface ButtonState {
  busy: string[];
  waiting: string[];
  lines: OutcomeLine[];
  /** Ждущие решения владельца нажатия сообщения — их карточку «Нажать?» открывает клик по кнопке. */
  pending: ButtonPress[];
}

/** Для рендера: читает реактивный стор, поэтому пересчитывается при каждом изменении нажатий. */
export function buttonState(messageId: number): ButtonState {
  const state = store.state.presses;
  const presses = Object.values(state.items).filter((p) => p.message_id === messageId);
  const { waiting, busy } = liveButtons(presses, messageId);
  const lines = outcomeLines(presses, messageId, Date.now());
  const prefix = `${messageId}:`;
  for (const key of Object.keys(state.sending)) {
    if (!key.startsWith(prefix)) continue;
    const [, row, col] = key.split(':');
    const addr = `${row}.${col}`;
    if (!busy.includes(addr)) busy.push(addr);
  }
  for (const [key, text] of Object.entries(state.errors)) {
    if (key.startsWith(prefix)) lines.push({ key: `err-${key}`, kind: 'warn', text, url: null });
  }
  const pending: ButtonPress[] = [];
  for (const p of presses) if (p.status === 'pending') pending.push(p);
  return { busy, waiting, lines, pending };
}
