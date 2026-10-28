// Ф38-2 (`docs/architecture.md`): заявки сессий на правило раскладки.
// Чистый редьюсер без Vue (как `joins.ts`): стор передаёт сюда `reactive`-объект.
import type { RuleRequest } from '../api/types';

export interface RuleRequestState {
  items: Record<number, RuleRequest>;
  error: string | null;
}

export function createRuleRequestState(): RuleRequestState {
  return { items: {}, error: null };
}

/** Заявка из `/sync` или REST: старшая по `seq` побеждает (опоздавший REST не откатывает свежее из `/sync`). */
export function applyRuleRequest(state: RuleRequestState, request: RuleRequest): void {
  const prev = state.items[request.id];
  if (prev !== undefined && prev.seq > request.seq) return;
  state.items[request.id] = request;
}
