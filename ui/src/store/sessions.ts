// Ф41-2б-2 (`docs/architecture.md`): состояние постоянных сессий хаба — записи, их ждущие
// запросы, очередь неотправленных реплик и загруженные события. Чистый редьюсер без Vue (как `work.ts`).
import type { HubSession, SessionEvent, SessionsToday, SessionEventDetail, SessionFileDraft, SessionLive, SessionRequest, SessionSay } from '../api/types';

export interface SessionsState {
  /** Незакрытые сессии по id. Закрытая — убирается. */
  items: Record<number, HubSession>;
  /** Ожидающие (`pending`) запросы по id сессии, по возрастанию id. */
  pending: Record<number, SessionRequest[]>;
  /** Реплики в очереди (`queued`) по id сессии, по возрастанию id. */
  says: Record<number, SessionSay[]>;
  /** События по id сессии, по возрастанию id (хвост + догруженное «раньше»). */
  events: Record<number, SessionEvent[]>;
  olderDone: Record<number, boolean>;
  /** Запросы, решение по которым получило 409 (протухли) — по id запроса. */
  stale: Record<number, SessionRequest>;
  /** Раскрытые окна сессий. */
  expanded: Record<number, boolean>;
  /** Ф41-3а: подробности шагов по ключу `<id сессии>:<id события>` — один запрос на раскрытие, дальше из стора. */
  details: Record<string, EventDetailEntry>;
  /** Ф41-3б: набираемый сейчас текст по id сессии (кадры `live`); в историю не попадает. */
  live: Record<number, SessionLive>;
  /** Ф41-3в: приложенное к следующей реплике по id сессии (живёт, пока страница не перезагружена). */
  files: Record<number, SessionFileDraft[]>;
  loadedAt: number | null;
  error: string | null;
  /** Ф41-6б: расход сессий хаба за сегодня (`GET /sessions` -> `today`); `null` — ядро не отдаёт. */
  today: SessionsToday | null;
}

export interface EventDetailEntry {
  status: 'loading' | 'ok' | 'error';
  detail: SessionEventDetail | null;
}

export function detailKey(sessionId: number, eventId: number): string {
  return `${sessionId}:${eventId}`;
}

export function createSessionsState(): SessionsState {
  return {
    items: {},
    pending: {},
    says: {},
    events: {},
    olderDone: {},
    stale: {},
    expanded: {},
    details: {},
    live: {},
    files: {},
    loadedAt: null,
    today: null,
    error: null,
  };
}

function forget(state: SessionsState, id: number): void {
  delete state.items[id];
  delete state.pending[id];
  delete state.says[id];
  delete state.events[id];
  delete state.olderDone[id];
  delete state.expanded[id];
  delete state.live[id];
  delete state.files[id];
  const prefix = `${id}:`;
  for (const key of Object.keys(state.details)) {
    if (key.startsWith(prefix)) delete state.details[key];
  }
}

/** Запись из `/sync` или REST: старшая по `seq` побеждает; `listening`, `workdir`, `cache` и
 * `engine_session_id` (их в элементе `/sync` нет) остаются прежними. Закрытая — прочь из списка. */
export function upsertSession(state: SessionsState, session: HubSession): void {
  if (session.closed_ts !== null) {
    forget(state, session.id);
    return;
  }
  const prev = state.items[session.id];
  if (prev !== undefined && prev.seq > session.seq) return;
  const next: HubSession = { ...session };
  if (prev !== undefined) {
    if (next.listening === undefined && prev.listening !== undefined) next.listening = prev.listening;
    if (next.workdir === undefined && prev.workdir !== undefined) next.workdir = prev.workdir;
    if (next.cache === undefined && prev.cache !== undefined) next.cache = prev.cache;
    if (next.agent_state === undefined && prev.agent_state !== undefined) next.agent_state = prev.agent_state;
    if (next.engine_session_id === undefined && prev.engine_session_id !== undefined) {
      next.engine_session_id = prev.engine_session_id;
    }
  }
  state.items[session.id] = next;
  // Приёмка 41-3: законченный блок (`done`) переживает конец хода — его сменит событие `text` с тем же `ref`
  // (или снимет срок 5 с); иначе текст на секунду пропадал между концом хода и догрузкой ленты.
  if (next.state !== 'running' && state.live[session.id]?.done !== true) delete state.live[session.id];
}

/** Запрос из `/sync`: `pending` — в список сессии, любой другой статус — прочь из списка. */
export function applySessionRequest(state: SessionsState, request: SessionRequest): void {
  const list = state.pending[request.session_id] ?? [];
  const idx = list.findIndex((r) => r.id === request.id);
  if (idx !== -1 && list[idx]!.seq > request.seq) return;
  if (request.status === 'pending') {
    if (idx === -1) list.push(request);
    else list[idx] = request;
    list.sort((a, b) => a.id - b.id);
  } else if (idx !== -1) {
    list.splice(idx, 1);
  }
  state.pending[request.session_id] = list;
}

/** Полный список с сервера (`GET /sessions`): записей, которых там нет, не остаётся; `cache` и `listening` свежие. */
export function replaceSessions(state: SessionsState, items: HubSession[]): void {
  const fresh = new Set(items.filter((s) => s.closed_ts === null).map((s) => s.id));
  for (const id of Object.keys(state.items).map(Number)) {
    if (!fresh.has(id)) forget(state, id);
  }
  for (const session of items) {
    if (session.closed_ts !== null) continue;
    const prev = state.items[session.id];
    // Более новая по `seq` запись из `/sync` не откатывается, но берёт из опроса то, чего в `/sync` нет.
    if (prev !== undefined && prev.seq > session.seq) {
      state.items[session.id] = {
        ...prev,
        listening: session.listening,
        cache: session.cache,
        agent_state: session.agent_state,
        workdir: session.workdir,
        engine_session_id: session.engine_session_id,
      };
    } else {
      state.items[session.id] = session;
    }
  }
}

export function setSessionPending(state: SessionsState, id: number, requests: SessionRequest[]): void {
  state.pending[id] = requests.filter((r) => r.status === 'pending').sort((a, b) => a.id - b.id);
}

export function setSessionSays(state: SessionsState, id: number, says: SessionSay[]): void {
  state.says[id] = [...says].sort((a, b) => a.id - b.id);
}

export function mergeSessionEvents(state: SessionsState, id: number, events: SessionEvent[]): void {
  const list = state.events[id] ?? [];
  const known = new Set(list.map((e) => e.id));
  for (const event of events) {
    if (!known.has(event.id)) {
      list.push(event);
      known.add(event.id);
    }
  }
  list.sort((a, b) => a.id - b.id);
  state.events[id] = list;
  // Ф41-3б: пришёл окончательный `text` с тем же `ref` — строка «пишет…» этого блока больше не нужна.
  const live = state.live[id];
  if (live !== undefined && events.some((e) => e.kind === 'text' && e.ref === live.block)) delete state.live[id];
}

/** Ф41-3б: кадр `live`. Возвращает `true`, если запись поставлена (а не отброшена как опоздавшая). */
export function applyLiveFrame(state: SessionsState, sessionId: number, frame: SessionLive): boolean {
  const known = state.events[sessionId];
  // Окончательный текст блока уже в ленте — опоздавший кадр строку не воскрешает.
  if (known !== undefined && known.some((e) => e.kind === 'text' && e.ref === frame.block)) return false;
  state.live[sessionId] = frame;
  return true;
}

export function lastSessionEventId(state: SessionsState, id: number): number {
  const list = state.events[id];
  return list !== undefined && list.length > 0 ? list[list.length - 1]!.id : 0;
}

export function firstSessionEventId(state: SessionsState, id: number): number | null {
  const list = state.events[id];
  return list !== undefined && list.length > 0 ? list[0]!.id : null;
}
