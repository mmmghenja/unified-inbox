// Ф17-1b (`docs/architecture.md`): заявки сессий на вступление в группы и каналы Telegram.
// Чистый редьюсер без Vue (как `work.ts`): стор передаёт сюда `reactive`-объект.
import type { JoinRequest } from '../api/types';

export interface JoinState {
  items: Record<number, JoinRequest>;
  error: string | null;
}

export function createJoinState(): JoinState {
  return { items: {}, error: null };
}

/** Заявка из `/sync` или REST: старшая по `seq` побеждает (опоздавший REST не откатывает свежее из `/sync`). */
export function applyJoin(state: JoinState, join: JoinRequest): void {
  const prev = state.items[join.id];
  if (prev !== undefined && prev.seq > join.seq) return;
  state.items[join.id] = join;
}
