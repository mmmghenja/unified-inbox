// Ф44-1d: нажатия кнопок ботов Telegram. Чистый редьюсер без Vue (как `joins.ts`): стор передаёт `reactive`-объект.
import type { ButtonPress } from '../api/types';

export interface PressState {
  items: Record<number, ButtonPress>;
  /** Кнопки (`messageId:row:col`), чей запрос на нажатие сейчас в пути. */
  sending: Record<string, true>;
  /** Отказы запроса на нажатие (нет строки в ядре) — текст под клавиатурой. */
  errors: Record<string, string>;
  error: string | null;
}

export function createPressState(): PressState {
  return { items: {}, sending: {}, errors: {}, error: null };
}

/** Адрес кнопки: id сообщения, ряд и место (с 1). */
export function pressKey(messageId: number, row: number, col: number): string {
  return `${messageId}:${row}:${col}`;
}

/** Нажатие из `/sync` или REST: старшая по `seq` побеждает (опоздавший REST не откатывает свежее из `/sync`). */
export function applyPress(state: PressState, press: ButtonPress): void {
  const prev = state.items[press.id];
  if (prev !== undefined && prev.seq > press.seq) return;
  // `result` (ответ бота) есть только у куки и автора; REST без него не должен стирать пришедшее из `/sync`.
  state.items[press.id] = prev !== undefined && press.result === undefined && prev.result !== undefined
    ? { ...press, result: prev.result }
    : press;
}
