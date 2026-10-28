// Ф50-4 (интерфейс): состояние сессии хаба — пометка о карточках, строка хода, `agent_state` не затирается `/sync`.
import { describe, expect, it } from 'vitest';

import type { AgentState, HubSession } from '../src/api/types';
import { createSessionsState, replaceSessions, upsertSession } from '../src/store/sessions';
import { agentStateParts, cardsNote } from '../src/util/agentState';

const NOW = new Date(2026, 9, 10, 15, 0, 0).getTime();

function ag(over: Partial<AgentState> = {}): AgentState {
  return {
    held: true,
    hub_session_id: 5,
    state: 'idle',
    turn: null,
    sleep: null,
    step: null,
    cards: { total: 0, user: 0 },
    signals: { waiting: 0, ready: 0, urgent: 0 },
    mid_queued_ts: null,
    bg: { running: 0, total: 0 },
    last_turn_ts: null,
    last_error: null,
    lines: [],
    ...over,
  };
}

function session(state?: AgentState, over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'секретарь', role_no: 1, engine: 'claude', model: null, effort: null, mode: 'auto',
    title: null, state: 'idle', host_seen_ts: null, created_ts: 1, updated_ts: 1, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null,
    pending_requests: 0, queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null,
    ...(state ? { agent_state: state } : {}),
    ...over,
  } as HubSession;
}

describe('cardsNote', () => {
  it('нет карточек и нет agent_state — пусто', () => {
    expect(cardsNote(session())).toBeNull();
    expect(cardsNote(session(ag()))).toBeNull();
  });
  it('карточки ждут владельца (делегата Ф49 больше нет)', () => {
    expect(cardsNote(session(ag({ cards: { total: 2, user: 2 } })))).toBe('ждёт тебя: 2');
  });
});

describe('agentStateParts', () => {
  it('идущий ход с источником и шагом', () => {
    const s = session(ag({ state: 'running', turn: { since_ts: NOW - 14 * 60_000, origin: 'signal' }, step: { tool: 'Bash', ts: NOW } }));
    expect(agentStateParts(s, NOW)).toEqual(['в работе 14 мин (по сигналу)', 'шаг: Bash']);
  });
  it('источники ходов словами', () => {
    const f = (origin: string) => agentStateParts(session(ag({ state: 'running', turn: { since_ts: NOW, origin } })), NOW)[0];
    expect(f('say')).toBe('в работе меньше минуты (по твоей реплике)');
    expect(f('self')).toContain('продолжает сама');
    expect(f('compact')).toContain('сжатие');
  });
  it('сигнал в очереди и ждущие сигналы', () => {
    const s = session(ag({ mid_queued_ts: NOW, signals: { waiting: 3, ready: 3, urgent: 1 } }));
    expect(agentStateParts(s, NOW)).toEqual(['сигнал подан, ждёт конца инструмента', 'сигналов ждёт: 3 (срочных 1)']);
    expect(agentStateParts(session(ag({ signals: { waiting: 2, ready: 2, urgent: 0 } })), NOW)).toEqual(['сигналов ждёт: 2']);
  });
  it('спит — причина текстом', () => {
    const s = session(ag({ state: 'asleep', sleep: { reason: 'heavy', text: 'спит: тяжёлая' } }));
    expect(agentStateParts(s, NOW)).toEqual(['спит: тяжёлая']);
  });
  it('нет agent_state — пусто', () => {
    expect(agentStateParts(session(), NOW)).toEqual([]);
  });
});

describe('agent_state и /sync', () => {
  it('запись из /sync без agent_state его не затирает', () => {
    const st = createSessionsState();
    upsertSession(st, session(ag({ signals: { waiting: 4, ready: 4, urgent: 0 } })));
    upsertSession(st, session(undefined, { seq: 11, state: 'running' }));
    expect(st.items[5]!.state).toBe('running');
    expect(st.items[5]!.agent_state?.signals.waiting).toBe(4);
  });
  it('replaceSessions: новее по seq запись из /sync берёт agent_state из опроса', () => {
    const st = createSessionsState();
    upsertSession(st, session(undefined, { seq: 20 }));
    replaceSessions(st, [session(ag({ signals: { waiting: 1, ready: 1, urgent: 0 } }), { seq: 10 })]);
    expect(st.items[5]!.seq).toBe(20);
    expect(st.items[5]!.agent_state?.signals.waiting).toBe(1);
  });
});
