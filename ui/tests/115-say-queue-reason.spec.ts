// Ф55-1 (интерфейс, раздел 3): серая строка-причина у блока «в очереди» окна сессии.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, SessionRequest, SessionSay } from '../src/api/types';
import SessionWindow from '../src/components/SessionWindow.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { sayQueueReason } from '../src/util/sayQueue';

const NOW = new Date(2026, 9, 13, 12, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: 'opus', effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 1, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

function say(over: Partial<SessionSay> = {}): SessionSay {
  return { id: 11, session_id: 5, text: 'сделай', priority: 'next', created_ts: NOW, status: 'queued', interrupted_ts: null, resent_ts: null, ...over };
}

const pendingReq = [{ status: 'pending' }] as SessionRequest[];

describe('sayQueueReason', () => {
  const q = [say()];
  it('нет реплик или сессия закрыта — строки нет', () => {
    expect(sayQueueReason(session({ state: 'running' }), [], [])).toBeNull();
    expect(sayQueueReason(session({ state: 'running', closed_ts: NOW }), [], q)).toBeNull();
  });
  it('ждущий запрос — вместе с ответом, даже когда ход идёт', () => {
    expect(sayQueueReason(session({ state: 'running' }), pendingReq, q)).toBe('уйдёт вместе с твоим ответом на запрос');
    expect(sayQueueReason(session({ state: 'running', pending_requests: 1 }), [], q)).toBe('уйдёт вместе с твоим ответом на запрос');
  });
  it('ход идёт — по движку', () => {
    expect(sayQueueReason(session({ state: 'running' }), [], q)).toBe('сессия возьмёт после текущего шага');
    expect(sayQueueReason(session({ state: 'running', engine: 'opencode' }), [], q)).toBe('уйдёт, когда закончится ход');
  });
  it('прерванная реплика — первой, выше запроса и хода', () => {
    const r = sayQueueReason(session({ state: 'running' }), pendingReq, [say(), say({ id: 12, status: 'interrupted' })]);
    expect(r).toBe('ждёт твоего решения по прерванной реплике');
  });
  it('причины ожидания — словами строки состояния', () => {
    expect(sayQueueReason(session({ wait_reason: 'paused' }), [], q)).toBe('пауза лимита подписки');
    expect(sayQueueReason(session({ wait_reason: 'compact' }), [], q)).toBe('ждёт конца сжатия');
    expect(sayQueueReason(session({ wait_reason: 'slot', state: 'asleep' }), [], q)).toBe('ждёт места');
  });
  it('спящая поднимается; свободная — без строки', () => {
    expect(sayQueueReason(session({ state: 'asleep' }), [], q)).toBe('сессия поднимается');
    expect(sayQueueReason(session({ state: 'idle' }), [], q)).toBeNull();
  });
});

describe('строка в окне сессии', () => {
  beforeEach(() => {
    api.setCsrf('c');
    api.setUnauthorizedHandler(null);
    resetBoard();
    store.state.sessions = createSessionsState();
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ session: session(), requests: [], says: [] }), { status: 200, headers: { 'content-type': 'application/json' } })));
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    document.body.innerHTML = '';
  });

  it('показывает причину под заголовком очереди и не считается репликой', async () => {
    const s = session({ state: 'running' });
    store.state.sessions.items[s.id] = s;
    store.state.sessions.says[s.id] = [say()];
    const w = mount(SessionWindow, { props: { session: s }, attachTo: document.body });
    await flushPromises();
    expect(w.get('.session-say-reason').text()).toBe('сессия возьмёт после текущего шага');
    expect(w.findAll('.session-say')).toHaveLength(1);
    w.unmount();
  });

  it('очереди нет — строки нет', async () => {
    const s = session({ state: 'running', queued_says: 0 });
    store.state.sessions.items[s.id] = s;
    const w = mount(SessionWindow, { props: { session: s }, attachTo: document.body });
    await flushPromises();
    expect(w.find('.session-say-reason').exists()).toBe(false);
    w.unmount();
  });
});
