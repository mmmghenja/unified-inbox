// Ф41-2б-2 (доработка по стенду): ждущие карточки постоянных сессий во «Ждёт», в счётчиках доски и вкладки,
// клик «Открыть сессию» ведёт к сессии; роль рядом с названием в строке сессии.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, SessionRequest } from '../src/api/types';
import SessionsBlock from '../src/components/SessionsBlock.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { buildBoard } from '../src/util/board';
import { startOfDay } from '../src/util/home';
import { sessionRequestLabel, sessionWaitCount } from '../src/util/sessions';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** Любой GET /sessions отдаёт `listed`; остальное — пустые ответы. */
function stubFetch(listed: HubSession[] = []): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/sessions') return jsonResponse({ items: listed });
      if (p === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
      if (p === '/api/v1/work/tasks') return jsonResponse([]);
      if (p === '/api/v1/outbox') return jsonResponse([]);
      return jsonResponse({});
    }),
  );
}

function session(overrides: Partial<HubSession>): HubSession {
  return {
    id: 5,
    project_id: 3,
    role: 'программист',
    role_no: 1,
    engine: 'claude',
    model: null,
    effort: null,
    mode: 'manual',
    title: null,
    state: 'running',
    host_seen_ts: NOW,
    created_ts: NOW,
    updated_ts: NOW,
    closed_ts: null,
    close_reason: null,
    created_by: 'ui',
    seq: 10,
    last_event_id: null,
    last_turn_ts: null,
    failure: null,
    wait_reason: null,
    pending_requests: 0,
    queued_says: 0,
    interrupt_requested: false,
    sleep_requested: false,
    sub_id: null,
    ...overrides,
  };
}

function sreq(overrides: Partial<SessionRequest>): SessionRequest {
  return {
    id: 100,
    session_id: 5,
    project_id: 3,
    kind: 'permission',
    tool: 'Bash',
    input: { command: 'ls' },
    input_truncated: false,
    reason: null,
    status: 'pending',
    answer: null,
    created_ts: NOW,
    decided_ts: null,
    delivered: false,
    seq: 11,
    ...overrides,
  };
}

function project(): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true };
}

function input() {
  return { projects: store.state.projects, home: { react: [], drafts: [] }, sessions: store.state.sessions };
}

beforeEach(() => {
  api.setCsrf('c');
  store.state.sessions = createSessionsState();
  store.state.projects = [project()];
  store.state.boardFocus = null;
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ждущие карточки сессий в «Ждёт» и счётчиках', () => {
  it('запрос сессии — свой элемент, подпись «сессия: просит tool» / «вопрос»; счётчик вкладки их считает', () => {
    store.state.sessions.items = { 5: session({ title: 'Разбор', pending_requests: 2 }) };
    store.state.sessions.pending = { 5: [sreq({ id: 100 }), sreq({ id: 101, kind: 'question', tool: 'AskUserQuestion' })] };
    const items = buildWaiting(input());
    expect(items.map((i) => i.kind)).toEqual(['session_request', 'session_request']);
    const labels = items.map((i) => (i.kind === 'session_request' ? sessionRequestLabel(i.session, i.request) : ''));
    expect(labels).toEqual(['Разбор: просит Bash', 'Разбор: вопрос']);
    expect(homeDecisionCount(input())).toBe(2);
  });

  it('список запросов не загружен — по pending_requests записи; загружен — по списку; без sessions как раньше', () => {
    store.state.sessions.items = { 5: session({ pending_requests: 2 }) };
    expect(buildWaiting(input())).toHaveLength(2);
    expect(sessionWaitCount(store.state.sessions, 3)).toBe(2);
    store.state.sessions.pending = { 5: [sreq({})] };
    expect(buildWaiting(input())).toHaveLength(1);
    expect(buildWaiting({ ...input(), sessions: undefined })).toHaveLength(0);
  });

  it('протухшая карточка остаётся; закрытая сессия не считается', () => {
    store.state.sessions.items = { 5: session({}), 6: session({ id: 6, closed_ts: NOW, pending_requests: 3 }) };
    store.state.sessions.stale = { 100: sreq({}) };
    const items = buildWaiting(input());
    expect(items).toHaveLength(1);
    expect(items[0]!.kind === 'session_request' && items[0]!.stale).toBe(true);
  });

  it('счётчик «ждёт» проекта на доске учитывает запросы сессий', () => {
    store.state.sessions.items = { 5: session({ pending_requests: 1 }) };
    const day = startOfDay(NOW);
    expect(buildBoard(store.state.projects, store.state.home, NOW).columns[0]!.waitCount).toBe(0);
    const col = buildBoard(store.state.projects, store.state.home, NOW, store.state.sessions).columns[0]!;
    expect(col.waitCount).toBe(1);
    expect(col.state.wait).toBe(true);
  });

  it('вкладка «Ждёт»: карточка видна, «Открыть сессию» раскрывает её и ведёт фокус к ней', async () => {
    stubFetch([session({ title: 'Разбор', pending_requests: 1 })]);
    store.state.sessions.items = { 5: session({ title: 'Разбор', pending_requests: 1 }) };
    store.state.sessions.pending = { 5: [sreq({})] };
    const w = mount(WaitList);
    await flushPromises();
    expect(w.text()).not.toContain('Ничего не ждёт');
    expect(w.get('.wait-session-title').text()).toBe('Разбор: просит Bash');
    await w.get('.wait-session-open').trigger('click');
    expect(store.state.sessions.expanded[5]).toBe(true);
    expect(store.state.boardFocus).toMatchObject({ projectId: 3, sessionId: 5 });
    w.unmount();
  });
});

describe('роль рядом с названием', () => {
  it('есть название и роль — роль с номером мелко; без названия отдельной роли нет', async () => {
    stubFetch([
      session({ id: 1, title: 'Разбор', role: 'программист', role_no: 2 }),
      session({ id: 2, title: null, role: 'тестер', role_no: 1 }),
    ]);
    const w = mount(SessionsBlock, { props: { projectId: 3 } });
    await flushPromises();
    const rows = w.findAll('.session-row');
    expect(rows[0]!.get('.session-role').text()).toBe('программист 2');
    expect(rows[1]!.find('.session-role').exists()).toBe(false);
    w.unmount();
  });
});
