// Ф54-1: заявки «нужен человек». Чистый редьюсер без Vue (как `presses.ts`): стор передаёт `reactive`-объект.
import type { HumanRequest } from '../api/types';

export interface HumanState {
  items: Record<number, HumanRequest>;
  error: string | null;
}

export function createHumanState(): HumanState {
  return { items: {}, error: null };
}

/** Заявка из `/sync` или REST: старшая по `seq` побеждает (опоздавший REST не откатывает свежее). */
export function applyHumanRequest(state: HumanState, item: HumanRequest): void {
  const prev = state.items[item.id];
  if (prev !== undefined && prev.seq > item.seq) return;
  state.items[item.id] = item;
}
