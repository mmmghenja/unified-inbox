// Ф48-2: прерванная реплика, «ждёт конца сжатия», окно «Сжатие…», «контекст N из M», один общий опрос «Ждёт тебя».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, SessionSay } from '../src/api/types';
import BoardProjectWait from '../src/components/BoardProjectWait.vue';
import BoardSession from '../src/components/BoardSession.vue';
import SessionCacheLine from '../src/components/SessionCacheLine.vue';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import { resetBoard } from '../src/store/board';
import { subscribeHomePoll } from '../src/store/homePoll';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { HOME_POLL_MS } from '../src/store/store';
import { sessionRow } from '../src/util/boardList';
import { buildCompactPatch, compactHint } from '../src/util/sessionCompact';
import { sessionStatusParts } from '../src/util/sessionStatus';
import { usageLabel } from '../src/util/sessionSteps';
import { INTERRUPTED_STATE, SESSION_WAIT_LABEL, interruptedWaitLabel, sessionWaitCount } from '../src/util/sessions';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 9, 9, 20, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: 'opus', effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

function say(over: Partial<SessionSay> = {}): SessionSay {
  return { id: 11, session_id: 5, text: 'сделай', priority: 'next', created_ts: NOW, status: 'queued', interrupted_ts: null, resent_ts: null, ...over };
}

type Call = { url: string; method: string; body: string | null };
let calls: Call[];
let nextResponse: { status: number; body: unknown };

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  calls = [];
  nextResponse = { status: 200, body: { session: session(), requests: [], says: [] } };
  store.state.sessions = createSessionsState();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null });
      return new Response(JSON.stringify(nextResponse.body), { status: nextResponse.status, headers: { 'content-type': 'application/json' } });
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

async function windowWith(s: HubSession, says: SessionSay[], column = false): Promise<ReturnType<typeof mount>> {
  store.state.sessions.items[s.id] = s;
  store.state.sessions.says[s.id] = says;
  const w = mount(SessionWindow, { props: { session: s, column }, attachTo: document.body });
  await flushPromises();
  calls.length = 0;
  return w;
}

describe('A: прерванная реплика', () => {
  it('пометка и обе кнопки зовут нужные ручки; у обычной реплики кнопок нет', async () => {
    const w = await windowWith(session({ queued_says: 2, interrupted_says: 1 }), [say({ status: 'interrupted', interrupted_ts: NOW }), say({ id: 12, text: 'потом' })]);
    const items = w.findAll('.session-say');
    expect(items).toHaveLength(2);
    expect(items[0]!.find('.session-say-interrupted').text()).toBe('прервана');
    expect(items[1]!.find('.session-say-resend').exists()).toBe(false);
    expect(items[1]!.find('.session-say-drop').exists()).toBe(false);

    await items[0]!.get('.session-say-resend').trigger('click');
    await flushPromises();
    expect(calls[0]).toMatchObject({ url: expect.stringContaining('/sessions/5/says/11/resend'), method: 'POST', body: '{}' });

    calls.length = 0;
    store.state.sessions.says[5] = [say({ status: 'interrupted', interrupted_ts: NOW })];
    await flushPromises();
    await w.get('.session-say-drop').trigger('click');
    await flushPromises();
    expect(calls[0]).toMatchObject({ url: expect.stringContaining('/sessions/5/says/11'), method: 'DELETE' });
    w.unmount();
  });

  it('Ф99-4: «Прервать» при непустой очереди предупреждает, что реплики останутся', async () => {
    const busy = await windowWith(session({ state: 'running' }), [say({ id: 12, text: 'а' }), say({ id: 13, text: 'б' })], true);
    expect(busy.get('.session-stop').attributes('title')).toBe('Прервётся текущий ход; реплик в очереди: 2 — останутся');
    busy.unmount();
    const empty = await windowWith(session({ state: 'running' }), [], true);
    expect(empty.get('.session-stop').attributes('title')).toBe('Прервать ход');
    empty.unmount();
  });

  it('отказ 409 показывается текстом окна', async () => {
    const w = await windowWith(session({ interrupted_says: 1 }), [say({ status: 'interrupted' })]);
    nextResponse = { status: 409, body: { detail: 'реплика не прервана' } };
    await w.get('.session-say-resend').trigger('click');
    await flushPromises();
    expect(w.get('.work-error').text()).toBe('реплика не прервана');
    w.unmount();
  });

  it('отказ 404 — тоже текстом, а не общей ошибкой сети', async () => {
    const w = await windowWith(session({ interrupted_says: 1 }), [say({ status: 'interrupted' })]);
    nextResponse = { status: 404, body: { detail: 'not found' } };
    await w.get('.session-say-drop').trigger('click');
    await flushPromises();
    expect(w.get('.work-error').text()).toBe('Реплики уже нет — окно обновилось.');
    w.unmount();
  });

  it('состояние «ждёт тебя: прерванная реплика» — в списке и в чипе столбца', () => {
    const s = session({ interrupted_says: 1, queued_says: 1 });
    expect(sessionRow(s, undefined, NOW)).toMatchObject({ sub: INTERRUPTED_STATE, dot: 'wait' });
    expect(sessionRow(session(), undefined, NOW).sub).toBe('ждёт входа');
    const w = mount(BoardSession, { props: { item: { kind: 'session', id: 5 }, session: s } });
    expect(w.get('.board-col-dot').attributes('title')).toBe(INTERRUPTED_STATE); // Ф90-1: подпись точки
    w.unmount();
  });

  it('строка в «Ждёт тебя»: «прерванная реплика — роль», счётчик её учитывает', () => {
    store.state.sessions.items = { 5: session({ interrupted_says: 1 }), 6: session({ id: 6, interrupted_says: 0 }) };
    const items = buildWaiting({
      projects: [{ id: 3, name: 'Umbrella', color: null } as never],
      home: { react: [], drafts: [] },
      
      sessions: store.state.sessions,
    });
    expect(items.filter((i) => i.kind === 'session_interrupted')).toHaveLength(1);
    expect(interruptedWaitLabel(session())).toBe('прерванная реплика — программист');
    expect(sessionWaitCount(store.state.sessions, 3)).toBe(1);
    expect(homeDecisionCount({
      projects: [], home: { react: [], drafts: [] }, sessions: store.state.sessions,
    })).toBe(1);
  });
});

describe('B: срочная ждёт конца сжатия', () => {
  it('подпись у срочной реплики только пока wait_reason = compact; причина расшифрована', async () => {
    expect(SESSION_WAIT_LABEL.compact).toBe('ждёт конца сжатия');
    const urgent = say({ priority: 'now' });
    const w = await windowWith(session({ queued_says: 1, wait_reason: 'compact' }), [urgent, say({ id: 12 })]);
    const items = w.findAll('.session-say');
    expect(items[0]!.find('.session-say-compact').text()).toBe('ждёт конца сжатия');
    expect(items[1]!.find('.session-say-compact').exists()).toBe(false);
    w.unmount();
    const plain = await windowWith(session({ queued_says: 1 }), [urgent]);
    expect(plain.find('.session-say-compact').exists()).toBe(false);
    plain.unmount();
  });
});

describe('D: окно «Сжатие…»', () => {
  async function openModal(s: HubSession): Promise<ReturnType<typeof mount>> {
    store.state.sessions.items[s.id] = s;
    const w = mount(SessionWindow, { props: { session: s }, attachTo: document.body });
    await flushPromises();
    await w.get('.session-more-toggle').trigger('click');
    await w.get('.session-more-compact').trigger('click');
    await flushPromises();
    calls.length = 0;
    return w;
  }

  it('PATCH только изменённого ключа; пусто — null; ×1000', async () => {
    const w = await openModal(session({ compact_tokens: 150_000, compact_auto: true }));
    const input = document.body.querySelector<HTMLInputElement>('.session-compact-input')!;
    expect(input.value).toBe('150');
    input.value = '300';
    input.dispatchEvent(new Event('input'));
    document.body.querySelector<HTMLFormElement>('.session-compact-modal')!.dispatchEvent(new Event('submit'));
    await flushPromises();
    expect(calls[0]).toMatchObject({ method: 'PATCH', body: '{"compact_tokens":300000}' });

    const again = await openModal(session({ compact_tokens: 150_000, compact_auto: true }));
    const field = document.body.querySelector<HTMLInputElement>('.session-compact-input')!;
    field.value = '';
    field.dispatchEvent(new Event('input'));
    const box = document.body.querySelector<HTMLInputElement>('.session-compact-auto-input')!;
    box.checked = false;
    box.dispatchEvent(new Event('change'));
    document.body.querySelector<HTMLFormElement>('.session-compact-modal')!.dispatchEvent(new Event('submit'));
    await flushPromises();
    expect(JSON.parse(calls.find((c) => c.method === 'PATCH')!.body!)).toEqual({ compact_tokens: null, compact_auto: false });
    w.unmount();
    again.unmount();
  });

  it('вне границ 50…900 — отказ на клиенте без запроса; чистая функция', () => {
    const s = { compact_tokens: null, compact_auto: true };
    expect(buildCompactPatch(s, true, '49').ok).toBe(false);
    expect(buildCompactPatch(s, true, '901').ok).toBe(false);
    expect(buildCompactPatch(s, true, '12,5').ok).toBe(false);
    expect(buildCompactPatch(s, true, '')).toEqual({ ok: true, body: {} });
    expect(buildCompactPatch(s, true, '50')).toEqual({ ok: true, body: { compact_tokens: 50_000 } });
  });

  it('вне границ в окне — текст ошибки, запроса нет; 422 ядра показывается как есть', async () => {
    const w = await openModal(session());
    const input = document.body.querySelector<HTMLInputElement>('.session-compact-input')!;
    input.value = '1000';
    input.dispatchEvent(new Event('input'));
    document.body.querySelector<HTMLFormElement>('.session-compact-modal')!.dispatchEvent(new Event('submit'));
    await flushPromises();
    expect(document.body.querySelector('.session-compact-modal .work-error')!.textContent).toContain('от 50 до 900');
    expect(calls).toHaveLength(0);
    input.value = '100';
    input.dispatchEvent(new Event('input'));
    nextResponse = { status: 422, body: { detail: 'порог вне границ' } };
    document.body.querySelector<HTMLFormElement>('.session-compact-modal')!.dispatchEvent(new Event('submit'));
    await flushPromises();
    expect(document.body.querySelector('.session-compact-modal .work-error')!.textContent).toBe('порог вне границ');
    w.unmount();
  });

  it('подсказка: контекст сейчас и порог; пункт меню «⋯» недоступен у закрытой сессии', () => {
    expect(compactHint(session({ usage: { context_tokens: 132_000, context_limit: 1_000_000, cost_usd: null, updated_ts: NOW }, compact_at: 200_000 })))
      .toBe('Сейчас в контексте 132 тыс.; сжатие при 200 тыс.');
    const open = mount(SessionMenuItems, { props: { win: null, session: session() } });
    expect(open.get('.session-menu-compact-settings').text()).toBe('Сжатие…');
    const closed = mount(SessionMenuItems, { props: { win: null, session: session({ closed_ts: NOW }) } });
    expect(closed.get('.session-menu-compact-settings').attributes('disabled')).toBeDefined();
  });
});

describe('«контекст N из M»', () => {
  const usage = { context_tokens: 132_000, context_limit: 1_000_000, cost_usd: null, updated_ts: NOW };

  it('M: compact_at хоста, иначе свой порог сессии, иначе 200', () => {
    expect(usageLabel(usage, { compact_at: 180_000, compact_tokens: 150_000, compact_auto: true }).context).toBe('контекст 132 тыс. из 180');
    expect(usageLabel(usage, { compact_at: null, compact_tokens: 150_000, compact_auto: true }).context).toBe('контекст 132 тыс. из 150');
    expect(usageLabel(usage, { compact_at: null, compact_tokens: null, compact_auto: true }).context).toBe('контекст 132 тыс. из 200');
  });

  it('без авто-сжатия — «· без авто-сжатия»; строка сессии и серая строка столбца', () => {
    expect(usageLabel(usage, { compact_auto: false }).context).toBe('контекст 132 тыс. · без авто-сжатия');
    const w = mount(SessionCacheLine, { props: { usage, compact: { compact_auto: true, compact_at: 200_000 } } });
    expect(w.text()).toBe('контекст 132 тыс. из 200');
    const s = session({ usage, compact_auto: false });
    expect(sessionStatusParts(s, NOW)).toContain('контекст 132 тыс. · без авто-сжатия');
    expect(sessionStatusParts(session({ usage, compact_at: 200_000 }), NOW)).toContain('контекст 132 тыс. из 200');
  });
});

describe('один общий опрос «Ждёт тебя»', () => {
  it('три блока столбцов и общая подписка — одна загрузка и один таймер', async () => {
    vi.useFakeTimers();
    const loadHome = vi.spyOn(store, 'loadHome').mockResolvedValue(undefined as never);
    vi.spyOn(store, 'loadSessions').mockResolvedValue(undefined as never);
    vi.spyOn(store, 'loadJoins').mockResolvedValue(undefined as never);
    vi.spyOn(store, 'loadRuleRequests').mockResolvedValue(undefined as never);
    vi.spyOn(store, 'loadAgentRequests').mockResolvedValue(undefined as never);
    vi.spyOn(store, 'loadPresses').mockResolvedValue(undefined as never);
    vi.spyOn(store, 'loadLeaves').mockResolvedValue(undefined as never);

    const blocks = [1, 2, 3].map((projectId) => mount(BoardProjectWait, { props: { projectId, count: 1 } }));
    await flushPromises();
    expect(loadHome).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(HOME_POLL_MS);
    expect(loadHome).toHaveBeenCalledTimes(2);

    blocks[0]!.unmount();
    blocks[1]!.unmount();
    await vi.advanceTimersByTimeAsync(HOME_POLL_MS);
    expect(loadHome).toHaveBeenCalledTimes(3); // последний подписчик ещё держит опрос
    blocks[2]!.unmount();
    await vi.advanceTimersByTimeAsync(HOME_POLL_MS * 3);
    expect(loadHome).toHaveBeenCalledTimes(3); // остановлен

    const again = subscribeHomePoll();
    expect(loadHome).toHaveBeenCalledTimes(4);
    again.release();
  });
});
