// Ф41-5 (интерфейс): заявки агентов (перенос беседы, напарник). Чистый редьюсер без Vue (как `ruleRequests.ts`).
import type { AgentRequest } from '../api/types';

export interface AgentRequestState {
  items: Record<number, AgentRequest>;
  error: string | null;
  /** Ядро ответило 404 на `/agent-requests` — до перезагрузки страницы не опрашиваем. */
  gone: boolean;
}

export function createAgentRequestState(): AgentRequestState {
  return { items: {}, error: null, gone: false };
}

/** Заявка из `/sync` или REST: старшая по `seq` побеждает (если `seq` не пришёл — последняя пришедшая). */
export function applyAgentRequest(state: AgentRequestState, request: AgentRequest): void {
  const prev = state.items[request.id];
  if (prev !== undefined && prev.seq !== undefined && request.seq !== undefined && prev.seq > request.seq) return;
  state.items[request.id] = request;
}
