// Ф41-2б-2 (`docs/architecture.md` + П13/П14): блок «Сессии проекта» и окно сессии —
// редьюсер и стор (наложение `/sync`, закрытие, события, очередь реплик), список, форма «Новая сессия» с отказом
// ядра, кнопки окна по состоянию, карточка запроса, «Прервать и отправить», обобщённые `WorkEventFeed`/`WorkRequestCard`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, SessionEvent, SessionRequest, SessionSay } from '../src/api/types';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import SessionsBlock from '../src/components/SessionsBlock.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import WorkEventFeed from '../src/components/WorkEventFeed.vue';
import { store } from '../src/store/instance';
import {
  applySessionRequest,
  createSessionsState,
  mergeSessionEvents,
  replaceSessions,
  upsertSession,
} from '../src/store/sessions';
import { sessionName, sessionTerminalCommand } from '../src/util/sessions';

vi.mock('../src/util/copy', () => ({ copyText: vi.fn(async () => true) }));
import { copyText } from '../src/util/copy';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  params: URLSearchParams;
  body: unknown;
}

type Handler = (method: string, path: string, params: URLSearchParams, body: unknown) => Response | undefined;

function fakeFetch(handler: Handler = () => undefined): { sent: Sent[]; fetchMock: ReturnType<typeof vi.fn> } {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, path: url.pathname, params: url.searchParams, body });
    const custom = handler(method, url.pathname, url.searchParams, body);
    if (custom !== undefined) return custom;
    const p = url.pathname;
    if (p === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (p === '/api/v1/status') {
      return jsonResponse({ accounts: [], dead_letter: {}, outbox_pending: 0, outbox_unknown: 0, max_seq: 1, now: 0 });
    }
    if (p === '/api/v1/sessions' && method === 'GET') return jsonResponse({ items: [] });
    if (/^\/api\/v1\/sessions\/\d+$/.test(p)) return jsonResponse({ session: session({}), requests: [], says: [] });
    if (/^\/api\/v1\/sessions\/\d+\/events$/.test(p)) return jsonResponse([]);
    return jsonResponse({ id: 1, status: 'ok' });
  });
  return { sent, fetchMock };
}

function posts(sent: Sent[], path: string): Sent[] {
  return sent.filter((r) => r.method === 'POST' && r.path === path);
}

function session(overrides: Partial<HubSession>): HubSession {
  return {
    id: 5,
    project_id: 3,
    role: 'программист',
    role_no: 1,
    engine: 'claude',
    model: 'sonnet',
    effort: null,
    mode: 'manual',
    title: null,
    state: 'idle',
    host_seen_ts: NOW,
    created_ts: NOW - 1000,
    updated_ts: NOW - 500,
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
    listening: false,
    engine_session_id: null,
    workdir: '/home/owner/proj',
    cache: null,
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
    input: { command: 'git push origin fix/x' },
    input_truncated: false,
    reason: 'гард хаба: git push',
    status: 'pending',
    answer: null,
    created_ts: NOW,
    decided_ts: null,
    delivered: false,
    seq: 11,
    ...overrides,
  };
}

function sevent(overrides: Partial<SessionEvent>): SessionEvent {
  return { id: 1, ts: NOW, kind: 'text', tool: null, summary: 'нашла', is_error: false, ref: null, ...overrides };
}

function say(overrides: Partial<SessionSay>): SessionSay {
  return { id: 1, session_id: 5, text: 'сделай', priority: 'next', created_ts: NOW, ...overrides };
}

function project(overrides: Partial<Project>): Project {
  return {
    id: 3,
    name: 'Umbrella',
    color: '#ff0000',
    sort: 0,
    muted: false,
    workdir: '/home/owner/proj',
    unread: 0,
    subs: true,
    ...overrides,
  };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sessions = createSessionsState();
  store.state.projects = [project({})];
  store.state.homeOpen = true;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// --- редьюсер ------------------------------------------------------------------------------------------------

describe('Ф41-2б-2: редьюсер сессий', () => {
  it('элемент /sync не затирает cache, listening, workdir; старший seq побеждает; закрытая уходит', () => {
    const st = createSessionsState();
    const cache = { last_ts: 1, ttl_s: 3600, expires_ts: NOW + 60_000, context_tokens: 5000, model: 'sonnet' };
    upsertSession(st, session({ seq: 10, cache, listening: true }));
    const synced = session({ seq: 12, state: 'running' });
    delete synced.cache;
    delete synced.listening;
    delete synced.workdir;
    upsertSession(st, synced);
    expect(st.items[5]!.state).toBe('running');
    expect(st.items[5]!.cache).toEqual(cache);
    expect(st.items[5]!.listening).toBe(true);
    expect(st.items[5]!.workdir).toBe('/home/owner/proj');
    upsertSession(st, session({ seq: 11, state: 'asleep' })); // опоздавшая
    expect(st.items[5]!.state).toBe('running');
    st.events[5] = [sevent({})];
    upsertSession(st, session({ seq: 13, closed_ts: NOW }));
    expect(st.items[5]).toBeUndefined();
    expect(st.events[5]).toBeUndefined();
  });

  it('запрос: pending — в список, любой другой статус — прочь', () => {
    const st = createSessionsState();
    applySessionRequest(st, sreq({ id: 2 }));
    applySessionRequest(st, sreq({ id: 1 }));
    expect(st.pending[5]!.map((r) => r.id)).toEqual([1, 2]);
    applySessionRequest(st, sreq({ id: 1, status: 'allowed', seq: 12 }));
    expect(st.pending[5]!.map((r) => r.id)).toEqual([2]);
  });

  it('GET /sessions заменяет список: пропавших нет, cache и listening свежие', () => {
    const st = createSessionsState();
    upsertSession(st, session({ id: 5 }));
    upsertSession(st, session({ id: 6 }));
    replaceSessions(st, [session({ id: 5, listening: true })]);
    expect(Object.keys(st.items)).toEqual(['5']);
    expect(st.items[5]!.listening).toBe(true);
  });

  it('события: слияние без дублей по возрастанию', () => {
    const st = createSessionsState();
    mergeSessionEvents(st, 5, [sevent({ id: 3 }), sevent({ id: 1 })]);
    mergeSessionEvents(st, 5, [sevent({ id: 2 }), sevent({ id: 3 })]);
    expect(st.events[5]!.map((e) => e.id)).toEqual([1, 2, 3]);
  });
});

// --- стор ----------------------------------------------------------------------------------------------------

describe('Ф41-2б-2: стор сессий', () => {
  it('/sync: запись и запрос ложатся в стор; раскрытая сессия дочитывает события по last_event_id', async () => {
    const { sent, fetchMock } = fakeFetch((method, path) => {
      if (path === '/api/v1/sync') {
        return jsonResponse({
          messages: [],
          channels: [],
          max_seq: 2,
          more: false,
          hub_sessions: [session({ seq: 20, last_event_id: 7, pending_requests: 1 })],
          session_requests: [sreq({ id: 9 })],
        });
      }
      if (path === '/api/v1/sessions/5/events') return jsonResponse([sevent({ id: 6 }), sevent({ id: 7 })]);
      if (path === '/api/v1/sessions/5') {
        return jsonResponse({ session: session({ seq: 20, last_event_id: 7, pending_requests: 1 }), requests: [sreq({ id: 9 })], says: [] });
      }
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.sessions.expanded[5] = true;
    store.state.sessions.events[5] = [sevent({ id: 5 })];
    await store.runSync();
    await flushPromises();
    expect(store.state.sessions.items[5]!.last_event_id).toBe(7);
    expect(store.state.sessions.pending[5]!.map((r) => r.id)).toEqual([9]);
    const ev = sent.find((r) => r.path === '/api/v1/sessions/5/events');
    expect(ev?.params.get('after')).toBe('5');
    expect(store.state.sessions.events[5]!.map((e) => e.id)).toEqual([5, 6, 7]);
  });

  it('/sync: закрытая сессия пропадает из списка', async () => {
    const { fetchMock } = fakeFetch((method, path) => {
      if (path === '/api/v1/sync') {
        return jsonResponse({
          messages: [],
          channels: [],
          max_seq: 3,
          more: false,
          hub_sessions: [session({ seq: 30, closed_ts: NOW })],
        });
      }
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.sessions.items[5] = session({ seq: 10 });
    await store.runSync();
    await flushPromises();
    expect(store.state.sessions.items[5]).toBeUndefined();
  });

  it('loadSessions: GET /sessions, опрос не стирает свежее; ошибка — строка, а не исключение', async () => {
    const { fetchMock } = fakeFetch((method, path) => {
      if (path === '/api/v1/sessions') return jsonResponse({ items: [session({ id: 7, title: 'Вторая' })] });
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    await store.loadSessions();
    expect(store.state.sessions.items[7]!.title).toBe('Вторая');
    vi.stubGlobal('fetch', vi.fn(async () => new Response('x', { status: 500 })));
    await store.loadSessions();
    expect(store.state.sessions.error).toMatch(/не загрузились/);
    expect(store.state.sessions.items[7]).toBeDefined();
  });

  it('первая загрузка событий — последние 30 до last_event_id + 1, дальше «раньше» — before первого', async () => {
    const page = Array.from({ length: 30 }, (_, i) => sevent({ id: 71 + i }));
    const { sent, fetchMock } = fakeFetch((method, path, params) => {
      if (path !== '/api/v1/sessions/5/events') return undefined;
      if (params.get('before') === '101') return jsonResponse(page);
      if (params.get('before') === '71') return jsonResponse([sevent({ id: 70 })]);
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.sessions.items[5] = session({ last_event_id: 100 });
    await store.loadSessionEvents(5);
    expect(store.state.sessions.events[5]).toHaveLength(30);
    expect(store.state.sessions.olderDone[5]).toBe(false);
    await store.loadOlderSessionEvents(5);
    expect(sent.some((r) => r.params.get('before') === '71' && r.params.get('limit') === '30')).toBe(true);
    expect(store.state.sessions.events[5]).toHaveLength(31);
    expect(store.state.sessions.olderDone[5]).toBe(true);
  });

  it('реплика: POST /say; 409 — готовая строка ядра; очередь перечитывается', async () => {
    const { sent, fetchMock } = fakeFetch((method, path) => {
      if (method === 'POST' && path === '/api/v1/sessions/5/say') {
        return jsonResponse({ detail: 'сессия хаба закрыта' }, 409);
      }
      if (path === '/api/v1/sessions/5') {
        return jsonResponse({ session: session({ queued_says: 1 }), requests: [], says: [say({ id: 4, text: 'жду' })] });
      }
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.sessions.items[5] = session({});
    const result = await store.saySession(5, 'привет', 'now');
    expect(posts(sent, '/api/v1/sessions/5/say')[0]!.body).toEqual({ text: 'привет', priority: 'now' });
    expect(result).toEqual({ ok: false, stale: true, message: 'сессия хаба закрыта' });
    expect(store.state.sessions.says[5]!.map((s) => s.text)).toEqual(['жду']);
  });

  it('закрытие: ответ 200 убирает сессию из списка сразу', async () => {
    vi.stubGlobal('fetch', fakeFetch().fetchMock);
    store.state.sessions.items[5] = session({});
    const result = await store.closeSession(5);
    expect(result.ok).toBe(true);
    expect(store.state.sessions.items[5]).toBeUndefined();
  });

  it('решение по запросу: POST /session-requests/{id}/decide; 409 — карточка протухла', async () => {
    const { sent, fetchMock } = fakeFetch((method, path) => {
      if (path === '/api/v1/session-requests/100/decide') {
        return jsonResponse({ detail: 'запрос уже решён или протух' }, 409);
      }
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    const r = sreq({});
    store.state.sessions.items[5] = session({});
    store.state.sessions.pending[5] = [r];
    const out = await store.decideSessionRequest(r, { decision: 'allow' });
    expect(out).toBe('stale');
    expect(posts(sent, '/api/v1/session-requests/100/decide')[0]!.body).toEqual({ decision: 'allow' });
    expect(store.state.sessions.stale[100]).toBeDefined();
    expect(store.state.sessions.pending[5]).toEqual([]);
  });
});

// --- подписи -------------------------------------------------------------------------------------------------

describe('Ф41-2б-2: подписи', () => {
  it('имя: название, иначе роль с номером, иначе номер сессии', () => {
    expect(sessionName(session({ title: ' Разбор ' }))).toBe('Разбор');
    expect(sessionName(session({ title: null, role: 'тестер', role_no: 2 }))).toBe('тестер 2');
    expect(sessionName(session({ title: null, role: 'тестер', role_no: 1 }))).toBe('тестер');
    expect(sessionName(session({ title: null, role: null, role_no: null, id: 9 }))).toBe('сессия #9');
  });

  it('команда терминала: только у спящей с id разговора; OpenCode — opencode -s', () => {
    expect(sessionTerminalCommand(session({ state: 'idle', engine_session_id: 'abc' }))).toBeNull();
    expect(sessionTerminalCommand(session({ state: 'asleep', engine_session_id: null }))).toBeNull();
    expect(sessionTerminalCommand(session({ state: 'asleep', engine_session_id: 'abc-1' }))).toBe(
      'cd /home/owner/proj && claude --resume abc-1',
    );
    expect(sessionTerminalCommand(session({ state: 'asleep', engine: 'opencode', engine_session_id: 'ses_x' }))).toBe(
      'cd /home/owner/proj && opencode -s ses_x',
    );
  });
});

// --- блок «Сессии проекта» -----------------------------------------------------------------------------------

describe('Ф41-2б-2: блок «Сессии проекта»', () => {
  it('строки: имя, движок, состояние, ждущие карточки, причина ожидания, failure, кэш; чужого проекта нет', async () => {
    const cache = { last_ts: NOW - 60_000, ttl_s: 3600, expires_ts: NOW + 600_000, context_tokens: 12_000, model: 'sonnet' };
    const all = [
      session({ id: 1, title: '<b>Разбор</b>', state: 'running', pending_requests: 2, cache }),
      session({ id: 2, role: 'тестер', role_no: 2, engine: 'opencode', state: 'asleep', wait_reason: 'elsewhere' }),
      session({ id: 3, state: 'idle', failure: 'не запустилась', wait_reason: 'slot' }),
      session({ id: 4, project_id: 99, title: 'Чужая' }),
    ];
    // Блок сам грузит список (`GET /sessions`) — отдаём те же записи.
    vi.stubGlobal(
      'fetch',
      fakeFetch((m, path) => (path === '/api/v1/sessions' ? jsonResponse({ items: all }) : undefined)).fetchMock,
    );
    const w = mount(SessionsBlock, { props: { projectId: 3, nowMs: NOW } });
    await flushPromises();
    const rows = w.findAll('.session-row');
    expect(rows).toHaveLength(3);
    expect(rows[0]!.text()).toContain('<b>Разбор</b>'); // только текстом
    expect(rows[0]!.find('b').exists()).toBe(false);
    expect(rows[0]!.text()).toContain('Claude Code');
    expect(rows[0]!.text()).toContain('в работе');
    expect(rows[0]!.text()).toContain('ждёт тебя: 2');
    expect(rows[0]!.find('.cache-line').exists()).toBe(true);
    expect(rows[1]!.text()).toContain('тестер 2');
    expect(rows[1]!.text()).toContain('OpenCode');
    expect(rows[1]!.text()).toContain('спит');
    expect(rows[1]!.text()).toContain('открыта в другом окне');
    expect(rows[2]!.text()).toContain('ждёт входа');
    expect(rows[2]!.text()).toContain('ждёт места');
    expect(rows[2]!.get('.session-failure').text()).toBe('не запустилась');
    expect(w.text()).not.toContain('Чужая');
    w.unmount();
  });

  it('строка раскрывается в окно; пустой список — «Сессий пока нет»', async () => {
    vi.stubGlobal('fetch', fakeFetch().fetchMock);
    const w = mount(SessionsBlock, { props: { projectId: 3 } });
    await flushPromises();
    expect(w.text()).toContain('Сессий пока нет');
    store.state.sessions.items = { 5: session({}) }; // добавилась по /sync
    await flushPromises();
    expect(w.find('.session-window').exists()).toBe(false);
    await w.get('button.session-row-head').trigger('click');
    await flushPromises();
    expect(w.find('.session-window').exists()).toBe(true);
    w.unmount();
  });

  it('блок держит опрос списка раз в 30 с, пока смонтирован', async () => {
    vi.useFakeTimers();
    const { sent, fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const w = mount(SessionsBlock, { props: { projectId: 3 } });
    await vi.advanceTimersByTimeAsync(0);
    const count = () => sent.filter((r) => r.method === 'GET' && r.path === '/api/v1/sessions').length;
    expect(count()).toBe(1);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(count()).toBe(2);
    w.unmount();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(count()).toBe(2);
  });

  it('создаёт сессию: роль, номер, движок, модель, название; пустое не уходит', async () => {
    const { sent, fetchMock } = fakeFetch((method, path) =>
      method === 'POST' && path === '/api/v1/sessions' ? jsonResponse({ session: session({ id: 8 }) }) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-role').setValue('тестер');
    await w.get('.session-new-number').setValue('2');
    await w.get('.session-new-model').setValue('opus');
    await w.get('.session-new-title').setValue('Проверка');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions')[0]!.body).toEqual({
      project_id: 3,
      engine: 'claude',
      role: 'тестер',
      number: 2,
      model: 'opus',
      title: 'Проверка',
      want_sub: { mode: 'direct' }, // Ф41-4: у сессии с ролью умолчание подписки — «только сообщения агентов»
    });
    expect(w.emitted('done')).toHaveLength(1);
    expect(store.state.sessions.items[8]).toBeDefined();
  });

  it('«усилие» у обоих движков, список по движку; при смене движка чужое значение сбрасывается; подсказки модели — по движку', async () => {
    const { sent, fetchMock } = fakeFetch((method, path) =>
      method === 'POST' && path === '/api/v1/sessions' ? jsonResponse({ session: session({ id: 9 }) }) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    expect(w.findAll('.session-new-effort option').map((o) => o.attributes('value'))).toEqual(['', 'low', 'medium', 'high', 'xhigh', 'max']);
    await w.get('.session-new-effort').setValue('xhigh');
    expect(w.findAll('datalist option').map((o) => o.attributes('value'))).toEqual(['sonnet', 'opus', 'haiku']);
    await w.get('.session-new-engine').setValue('opencode');
    expect((w.get('.session-new-effort').element as HTMLSelectElement).value).toBe(''); // xhigh у OpenCode нет
    expect(w.findAll('.session-new-effort option').map((o) => o.attributes('value'))).toEqual(['', 'low', 'high', 'max']);
    expect(w.findAll('datalist option').map((o) => o.attributes('value'))).toEqual(['deepseek/deepseek-flash', 'anthropic/claude-sonnet-5-5']);
    await w.get('.session-new-effort').setValue('max');
    await w.get('.session-new-model').setValue('deepseek/deepseek-flash');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions')[0]!.body).toEqual({
      project_id: 3,
      engine: 'opencode',
      model: 'deepseek/deepseek-flash',
      effort: 'max',
    });
  });

  it('отказ ядра (занятая линия) — его строкой под формой, форма не закрывается', async () => {
    vi.stubGlobal(
      'fetch',
      fakeFetch((method, path) =>
        method === 'POST' && path === '/api/v1/sessions'
          ? jsonResponse({ detail: 'линия «тестер 2» уже занята' }, 409)
          : undefined,
      ).fetchMock,
    );
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-number').setValue('2');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.get('[role="alert"]').text()).toBe('линия «тестер 2» уже занята');
    expect(w.emitted('done')).toBeUndefined();
  });

  it('номер не число — отправки нет', async () => {
    const { sent, fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-number').setValue('abc');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions')).toHaveLength(0);
  });
});

// --- окно сессии ---------------------------------------------------------------------------------------------

describe('Ф41-2б-2: окно сессии', () => {
  function open(overrides: Partial<HubSession>, fetchImpl = fakeFetch()) {
    vi.stubGlobal('fetch', fetchImpl.fetchMock);
    const s = session(overrides);
    store.state.sessions.items[s.id] = s;
    const w = mount(SessionWindow, { props: { session: s } });
    return { w, ...fetchImpl };
  }

  it('кнопки по состоянию: «Прервать» и «Прервать и отправить» — только running; «Усыпить» — кроме спящей с пустой очередью', async () => {
    const run = open({ state: 'running' });
    await run.w.get('textarea').setValue('привет');
    expect(run.w.get('.session-interrupt').attributes('disabled')).toBeUndefined();
    expect(run.w.get('.work-say-now').attributes('disabled')).toBeUndefined();
    run.w.unmount();
    for (const state of ['idle', 'asleep'] as const) {
      const other = open({ state });
      await other.w.get('textarea').setValue('привет');
      expect(other.w.get('.session-interrupt').attributes('disabled')).toBeDefined();
      expect(other.w.get('.work-say-now').attributes('disabled')).toBeDefined();
      expect(other.w.get('.work-say-next').attributes('disabled')).toBeUndefined();
      // Ф41-3 (2.6 п.3): спящей сессии с пустой очередью усыплять нечего.
      if (state === 'asleep') expect(other.w.get('.session-sleep').attributes('disabled')).toBeDefined();
      else expect(other.w.get('.session-sleep').attributes('disabled')).toBeUndefined();
      other.w.unmount();
    }
  });

  it('просьбы ядра: «прерываю…» и «усыпляю…» вместо кнопок', () => {
    const { w } = open({ state: 'running', interrupt_requested: true, sleep_requested: true });
    expect(w.get('.session-interrupt').text()).toBe('прерываю…');
    expect(w.get('.session-sleep').text()).toBe('усыпляю…');
    expect(w.get('.session-interrupt').attributes('disabled')).toBeDefined();
    expect(w.get('.session-sleep').attributes('disabled')).toBeDefined();
    w.unmount();
  });

  it('«Отправить» — priority next; «Прервать и отправить» — now; поле очищается, при отказе возвращается', async () => {
    let fail = false;
    const { w, sent } = open(
      { state: 'running' },
      fakeFetch((method, path) =>
        method === 'POST' && path === '/api/v1/sessions/5/say' && fail
          ? jsonResponse({ detail: 'text: пустая реплика' }, 422)
          : undefined,
      ),
    );
    await w.get('textarea').setValue('один');
    await w.get('.work-say-next').trigger('click');
    await flushPromises();
    await w.get('textarea').setValue('два');
    await w.get('.work-say-now').trigger('click');
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions/5/say').map((r) => r.body)).toEqual([
      { text: 'один', priority: 'next' },
      { text: 'два', priority: 'now' },
    ]);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('');
    fail = true;
    await w.get('textarea').setValue('три');
    await w.get('.work-say-next').trigger('click');
    await flushPromises();
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('три');
    expect(w.get('[role="alert"]').text()).toBe('text: пустая реплика');
    w.unmount();
  });

  it('Ctrl+Enter в поле = «Отправить»', async () => {
    const { w, sent } = open({ state: 'idle' });
    await w.get('textarea').setValue('быстро');
    await w.get('textarea').trigger('keydown', { key: 'Enter', ctrlKey: true });
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions/5/say')).toHaveLength(1);
    w.unmount();
  });

  it('очередь неотправленных реплик — списком над полем ввода', async () => {
    const { w } = open({ queued_says: 2 });
    store.state.sessions.says[5] = [say({ id: 1, text: 'первая' }), say({ id: 2, text: 'вторая', priority: 'now' })];
    await flushPromises();
    const items = w.findAll('.session-say');
    expect(items.map((i) => i.text())).toEqual(['в очереди: первая', 'в очереди, срочно: вторая']);
    w.unmount();
  });

  it('«Усыпить» и «Прервать» зовут ручки; «Закрыть» — после подтверждения', async () => {
    const { w, sent } = open({ state: 'running' });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    await w.get('.session-interrupt').trigger('click');
    await flushPromises();
    await w.get('.session-sleep').trigger('click');
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions/5/interrupt')).toHaveLength(1);
    expect(posts(sent, '/api/v1/sessions/5/sleep')).toHaveLength(1);
    await w.get('.session-close').trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenLastCalledWith('Сессия закроется насовсем: подписка и ждущее пропадут');
    expect(posts(sent, '/api/v1/sessions/5/close')).toHaveLength(0);
    await w.get('.session-close').trigger('click');
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions/5/close')).toHaveLength(1);
    w.unmount();
  });

  it('отказ ядра на «Усыпить» (закрыта) — его строкой', async () => {
    const { w } = open(
      { state: 'idle' },
      fakeFetch((method, path) =>
        method === 'POST' && path === '/api/v1/sessions/5/sleep' ? jsonResponse({ detail: 'сессия хаба закрыта' }, 409) : undefined,
      ),
    );
    await w.get('.session-sleep').trigger('click');
    await flushPromises();
    expect(w.get('[role="alert"]').text()).toBe('сессия хаба закрыта');
    w.unmount();
  });

  it('«Открыть в терминале» — только у спящей с id разговора; копирует команду и напоминает про одно окно', async () => {
    const idle = open({ state: 'idle', engine_session_id: 'abc' });
    expect(idle.w.find('.session-terminal').exists()).toBe(false);
    idle.w.unmount();
    const none = open({ state: 'asleep', engine_session_id: null });
    expect(none.w.find('.session-terminal').exists()).toBe(false);
    none.w.unmount();
    const { w } = open({ state: 'asleep', engine_session_id: 'abc-1' });
    await w.get('.session-terminal').trigger('click');
    await flushPromises();
    expect(copyText).toHaveBeenCalledWith('cd /home/owner/proj && claude --resume abc-1');
    expect(w.get('.work-terminal-command').text()).toBe('cd /home/owner/proj && claude --resume abc-1');
    expect(w.get('.work-terminal-note').text()).toContain('одна сессия — одно окно');
    w.unmount();
  });

  it('строка подписки и красная строка failure', () => {
    const { w } = open({ failure: 'claude не найден' });
    expect(w.get('.session-notice').text()).toContain('не подписана'); // Ф41-4: вместо «Сигналы… пока не приходят»
    expect(w.get('.session-failure').text()).toBe('claude не найден');
    w.unmount();
  });

  it('карточка разрешения: «Разрешить» -> /session-requests/{id}/decide', async () => {
    const { w, sent } = open({ state: 'running', pending_requests: 1 });
    store.state.sessions.pending[5] = [sreq({})];
    await flushPromises();
    expect(w.get('.work-request-command').text()).toBe('git push origin fix/x');
    await w.get('.work-allow').trigger('click');
    await flushPromises();
    expect(posts(sent, '/api/v1/session-requests/100/decide')[0]!.body).toEqual({ decision: 'allow' });
    expect(posts(sent, '/api/v1/work/requests/100/decide')).toHaveLength(0);
    w.unmount();
  });

  it('вопрос сессии: ответ уходит в answers', async () => {
    const { w, sent } = open({ state: 'running', pending_requests: 1 });
    store.state.sessions.pending[5] = [
      sreq({
        id: 101,
        kind: 'question',
        tool: 'AskUserQuestion',
        input: { questions: [{ question: 'Куда?', options: [{ label: 'Туда' }, { label: 'Сюда' }] }] },
      }),
    ];
    await flushPromises();
    await w.get('.work-question-option').trigger('click');
    await w.get('.work-allow').trigger('click');
    await flushPromises();
    expect(posts(sent, '/api/v1/session-requests/101/decide')[0]!.body).toEqual({
      decision: 'allow',
      answers: { 'Куда?': 'Туда' },
    });
    w.unmount();
  });

  it('протухшая карточка: «разрешено, повтори» — реплика в очередь этой сессии', async () => {
    const { w, sent } = open({ state: 'running' });
    store.state.sessions.stale[100] = sreq({});
    await flushPromises();
    expect(w.get('.work-request-stale').text()).toBe('запрос протух');
    const retry = w.findAll('.work-request-actions button').find((b) => b.text().startsWith('Написать'))!;
    await retry.trigger('click');
    await flushPromises();
    expect(posts(sent, '/api/v1/sessions/5/say')[0]!.body).toEqual({ text: 'Разрешено, повтори.', priority: 'next' });
    expect(posts(sent, '/api/v1/work/tasks/5/say')).toHaveLength(0);
    expect(store.state.sessions.stale[100]).toBeUndefined();
    w.unmount();
  });

  it('лента: события сессии из стора, первая загрузка и «показать раньше»', async () => {
    const page = Array.from({ length: 30 }, (_, i) => sevent({ id: 71 + i }));
    const { sent, w } = open(
      { last_event_id: 100 },
      fakeFetch((method, path, params) => {
        if (path !== '/api/v1/sessions/5/events') return undefined;
        if (params.get('before') === '101') return jsonResponse(page);
        if (params.get('before') === '71') return jsonResponse([sevent({ id: 70, kind: 'say', summary: 'владелец (next): привет' })]);
        return jsonResponse([]);
      }),
    );
    await flushPromises();
    expect(w.findAll('.work-event')).toHaveLength(30);
    await w.get('button.work-events-older').trigger('click');
    await flushPromises();
    expect(sent.some((r) => r.params.get('before') === '71')).toBe(true);
    expect(w.findAll('.work-event')).toHaveLength(31);
    expect(w.findAll('.work-event')[0]!.text()).toBe('ты написал: привет');
    expect(w.find('button.work-events-older').exists()).toBe(false);
    w.unmount();
  });
});

// --- обобщённые компоненты -----------------------------------------------------------------------------------

describe('Ф41-2б-2: WorkEventFeed с пропсами', () => {
  it('рисует переданные события и зовёт колбэки; стор задач не нужен', async () => {
    const load = vi.fn(async () => {});
    const loadOlder = vi.fn(async () => {});
    const w = mount(WorkEventFeed, {
      props: {
        events: [sevent({ id: 1, kind: 'interrupt', summary: 'прервано' }), sevent({ id: 2, kind: 'compact', summary: 'сжато' })],
        olderDone: false,
        load,
        loadOlder,
      },
    });
    await flushPromises();
    expect(load).toHaveBeenCalledTimes(1);
    expect(w.findAll('.work-event').map((e) => e.text())).toEqual(['прервано', 'сжато']);
    await w.get('button.work-events-older').trigger('click');
    expect(loadOlder).toHaveBeenCalledTimes(1);
    await w.setProps({ olderDone: true });
    expect(w.find('button.work-events-older').exists()).toBe(false);
    w.unmount();
  });
});
