// Ф56-3: очередь владельца (окно «Агент»). Чистое состояние без Vue (как `humanRequests.ts`): стор передаёт
// `reactive`-объект. Снимок целиком заменяет прежний — ядро отдаёт полную очередь, дельт нет.
import type { OwnerItem, OwnerQueue } from '../api/types';

export interface OwnerState {
  items: OwnerItem[];
  seq: number;
  agent: { online: boolean; seen_ts: number | null; talk_unread: number; talk_open: number; talk_progress: number };
  loaded: boolean;
  loading: boolean;
  error: string | null;
  /** «Уже решено» после 409/404: карточка из очереди ушла, сказать об этом больше негде. */
  notice: string | null;
}

export function createOwnerState(): OwnerState {
  return { items: [], seq: 0, agent: { online: false, seen_ts: null, talk_unread: 0, talk_open: 0, talk_progress: 0 }, loaded: false, loading: false, error: null, notice: null };
}

/** Применить снимок; снимок старее уже применённого (`seq` меньше) не откатывает. */
export function applyOwnerQueue(state: OwnerState, queue: OwnerQueue): void {
  if (state.loaded && typeof queue.seq === 'number' && queue.seq < state.seq) return;
  state.items = Array.isArray(queue.items) ? queue.items : [];
  state.seq = typeof queue.seq === 'number' ? queue.seq : state.seq;
  state.agent = {
    online: queue.agent?.online === true,
    seen_ts: typeof queue.agent?.seen_ts === 'number' ? queue.agent.seen_ts : null,
    talk_unread: typeof queue.agent?.talk_unread === 'number' ? queue.agent.talk_unread : 0,
    talk_open: typeof queue.agent?.talk_open === 'number' ? queue.agent.talk_open : 0,
    talk_progress: typeof queue.agent?.talk_progress === 'number' ? queue.agent.talk_progress : 0,
  };
  state.loaded = true;
  state.error = null;
}
