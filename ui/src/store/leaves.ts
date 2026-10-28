// Ф17-2b (`docs/architecture.md`): заявки на выход из групп и каналов Telegram.
// Чистый редьюсер без Vue (как `joins.ts`): стор передаёт сюда `reactive`-объект.
import type { LeaveRequest } from '../api/types';

export interface LeaveState {
  items: Record<number, LeaveRequest>;
  error: string | null;
}

export function createLeaveState(): LeaveState {
  return { items: {}, error: null };
}

/** Заявка из `/sync` или REST: старшая по `seq` побеждает. */
export function applyLeave(state: LeaveState, leave: LeaveRequest): void {
  const prev = state.items[leave.id];
  if (prev !== undefined && prev.seq > leave.seq) return;
  state.items[leave.id] = leave;
}
