// Ф62-6: заявки на группы WhatsApp. Чистый редьюсер без Vue (как `leaves.ts`): стор передаёт `reactive`-объект.
import type { GroupOp } from '../api/types';

export interface GroupOpState {
  items: Record<number, GroupOp>;
  error: string | null;
}

export function createGroupOpState(): GroupOpState {
  return { items: {}, error: null };
}

/** Заявка из `/sync` или REST: старшая по `seq` побеждает. */
export function applyGroupOp(state: GroupOpState, op: GroupOp): void {
  const prev = state.items[op.id];
  if (prev !== undefined && prev.seq > op.seq) return;
  state.items[op.id] = op;
}
