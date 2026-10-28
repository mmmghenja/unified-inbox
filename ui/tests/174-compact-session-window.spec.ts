// Ф90-1 (docs/architecture.md, Р1/Р2/Р5): окно сессии — одна строка сверху и одна снизу, остальное в «⋯».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, SessionRequest } from '../src/api/types';
import SessionHeadBar from '../src/components/SessionHeadBar.vue';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import WorkEventFeed from '../src/components/WorkEventFeed.vue';
import WorkRequestCard from '../src/components/WorkRequestCard.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { sessionHeadState } from '../src/util/sessionHead';

const NOW = new Date(2026, 9, 24, 10, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: 'opus', effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

function request(id: number): SessionRequest {
  return {
    id, session_id: 5, project_id: 3, kind: 'permission', tool: 'Bash', input: { command: `echo ${id}` }, input_truncated: false,
    reason: null, status: 'pending', answer: null, created_ts: 1, decided_ts: null, delivered: false,
  } as SessionRequest;
}

type Call = { url: string; method: string; body: string | null };
let calls: Call[];

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  calls = [];
  store.state.sessions = createSessionsState();
  store.state.projects = [{ id: 3, name: 'Umbrella', color: null, sort: 0, muted: false, workdir: '/p', unread: 0, subs: true } as never];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method ?? 'GET', body: typeof init?.body === 'string' ? init.body : null });
      return new Response(JSON.stringify({ session: session(), requests: [], says: [] }), { status: 200, headers: { 'content-type': 'application/json' } });
    }),
  );
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

async function windowWith(s: HubSession, pending: SessionRequest[] = []): Promise<ReturnType<typeof mount>> {
  store.state.sessions.items[s.id] = s;
  store.state.sessions.pending[s.id] = pending;
  const w = mount(SessionWindow, { props: { session: s, column: true }, attachTo: document.body });
  await flushPromises();
  calls.length = 0;
  return w;
}

describe('точка состояния вместо чипа', () => {
  it('ждёт / работает / спит / не слушает подписку', () => {
    expect(sessionHeadState(session({ pending_requests: 1 }), 1)).toMatchObject({ tone: 'wait', text: 'ждёт тебя' });
    expect(sessionHeadState(session({ state: 'running' }), 0).tone).toBe('run');
    expect(sessionHeadState(session({ state: 'asleep' }), 0)).toMatchObject({ tone: 'idle', text: 'спит' });
    expect(sessionHeadState(session({ state: 'idle', sub: null }), 0)).toMatchObject({ tone: 'silent', text: 'не слушает подписку' });
  });
});

describe('шапка в одну строку', () => {
  it('имя, точка, «авто» и «⋯»; проекта, модели и машины в шапке нет — они в «Сведениях»', async () => {
    const win = { busy: false, chooseMode: vi.fn(async () => {}) } as never;
    const w = mount(SessionHeadBar, { props: { session: session({ mode: 'auto', state: 'running' }), win, back: true }, attachTo: document.body });
    expect(w.get('.phone-session-title').text()).toBe('программист');
    expect(w.get('.session-head-dot').attributes('title')).toBe('работает');
    expect(w.find('.session-model-chip, .machine-chip, .phone-session-project').exists()).toBe(false);
    await w.get('.session-head-auto').trigger('click');
    expect((win as { chooseMode: ReturnType<typeof vi.fn> }).chooseMode).toHaveBeenCalledWith('manual');
    await w.get('.phone-session-menu-button').trigger('click');
    const info = w.get('.session-menu-info').text();
    expect(info).toContain('Umbrella');
    expect(info).toContain('opus');
    w.unmount();
  });

  it('в «⋯» пункт режима зовёт chooseMode и «Подписка…» открывает правку', async () => {
    const win = { busy: false, chooseMode: vi.fn(async () => {}), toggleSubEdit: vi.fn(), nothingToSleep: false, canTerminal: false } as never;
    const w = mount(SessionMenuItems, { props: { win, session: session() } });
    await w.get('.session-menu-mode').trigger('click');
    expect((win as { chooseMode: ReturnType<typeof vi.fn> }).chooseMode).toHaveBeenCalledWith('auto');
    await w.get('.session-menu-sub').trigger('click');
    expect((win as { toggleSubEdit: ReturnType<typeof vi.fn> }).toggleSubEdit).toHaveBeenCalled();
    expect(w.get('.session-menu-sub').text()).toBe('Подписка…');
  });
});

describe('низ в одну строку', () => {
  it('нет строки подписки, переключателя «Ручной | Авто» и подсказки Ctrl+Enter; ➤ отправляет', async () => {
    const w = await windowWith(session());
    expect(w.find('.session-sub-line').exists()).toBe(false);
    expect(w.find('.session-mode').exists()).toBe(false);
    expect(w.find('.submit-hint').exists()).toBe(false);
    expect(w.findAll('.work-say-row > *').length).toBeLessThanOrEqual(4);
    await w.get('textarea.work-say-text').setValue('привет');
    await w.get('.work-say-next').trigger('click');
    await flushPromises();
    expect(calls.find((c) => c.url.includes('/sessions/5/say'))?.body).toContain('"priority":"next"');
    w.unmount();
  });

  it('строка работы — только пока идёт ход', async () => {
    const idle = await windowWith(session({ agent_state: { state: 'idle' } as never }));
    expect(idle.find('.session-statusline').exists()).toBe(false);
    idle.unmount();
    const running = await windowWith(
      session({ state: 'running', agent_state: { state: 'running', turn: { since_ts: Date.now(), origin: 'ui' }, step: { tool: 'Read' } } as never }),
    );
    expect(running.get('.session-statusline').text()).toContain('шаг: Read');
    running.unmount();
  });

  it('«Прервать и отправить» — в меню у ➤ и по Ctrl+Shift+Enter; не идёт ход — меню нет', async () => {
    const idle = await windowWith(session());
    expect(idle.find('.work-say-more').exists()).toBe(false);
    idle.unmount();
    const w = await windowWith(session({ state: 'running' }));
    expect(w.find('.work-say-now').exists()).toBe(false);
    await w.get('textarea.work-say-text').setValue('срочно');
    await w.get('.work-say-more').trigger('click');
    await w.get('.work-say-now').trigger('click');
    await flushPromises();
    expect(calls.find((c) => c.url.includes('/sessions/5/say'))?.body).toContain('"priority":"now"');
    calls.length = 0;
    await w.get('textarea.work-say-text').setValue('ещё срочнее');
    await w.get('textarea.work-say-text').trigger('keydown', { key: 'Enter', ctrlKey: true, shiftKey: true });
    await flushPromises();
    expect(calls.find((c) => c.url.includes('/sessions/5/say'))?.body).toContain('"priority":"now"');
    w.unmount();
  });
});

describe('карточки запросов', () => {
  it('видны две, остальные — под «ещё N»', async () => {
    const w = await windowWith(session({ pending_requests: 3 }), [request(1), request(2), request(3)]);
    expect(w.findAll('.work-request')).toHaveLength(2);
    expect(w.get('.session-cards-more').text()).toBe('ещё 1');
    await w.get('.session-cards-more').trigger('click');
    expect(w.findAll('.work-request')).toHaveLength(3);
    expect(w.find('.session-cards-more').exists()).toBe(false);
    w.unmount();
  });

  it('свёрнутая в окне — «⌄» и без «открыть окном»; развёрнутая — с ним', async () => {
    const handlers = { decide: async () => 'ok' as const, retry: async () => 'ok' as const, dismiss: () => undefined };
    const w = mount(WorkRequestCard, { props: { ...handlers, request: request(1), compact: true, oneLine: true } });
    expect(w.get('button.work-request-expand').text()).toBe('⌄');
    expect(w.find('button.work-request-open').exists()).toBe(false);
    await w.get('button.work-request-expand').trigger('click');
    expect(w.find('button.work-request-open').exists()).toBe(true);
  });
});

describe('лента: «показать раньше» подгружается при прокрутке к верху', () => {
  it('пользовательская прокрутка к верху зовёт loadOlder; без прокрутки — нет', async () => {
    const loadOlder = vi.fn(async () => {});
    const events = [{ id: 1, kind: 'text', ts: 1, summary: 'a', tool: null, is_error: false, ref: null }] as never;
    const w = mount(WorkEventFeed, { props: { events, olderDone: false, loadOlder, sessionId: 5 }, attachTo: document.body });
    await flushPromises();
    const list = w.get('.work-events-list');
    const el = list.element as HTMLElement;
    Object.defineProperty(el, 'scrollHeight', { value: 1000, configurable: true });
    Object.defineProperty(el, 'clientHeight', { value: 300, configurable: true });
    el.scrollTop = 0;
    await list.trigger('scroll');
    expect(loadOlder).not.toHaveBeenCalled(); // сам не листал
    await list.trigger('wheel', { deltaY: -50 });
    await list.trigger('scroll');
    await flushPromises();
    expect(loadOlder).toHaveBeenCalledTimes(1);
    w.unmount();
  });
});

describe('телефон (Р2): полоса раздела и нижние вкладки', () => {
  function mountShell() {
    return mount(PhoneShell, {
      attachTo: document.body,
      global: {
        stubs: {
          TopMenu: true, WaitList: true, HomeView: true, ChatsView: true, ClaudeView: true, AgentQueue: true, NewMessageButton: true, SearchBox: true,
          PhoneSessions: { template: '<textarea class="stub-field"></textarea><button class="stub-btn">b</button>' },
        },
      },
    });
  }

  it('в открытой сессии полосы раздела нет; клавиатура (фокус в поле) прячет вкладки, уход фокуса возвращает', async () => {
    store.state.phoneTab = 'orch';
    store.state.phoneSession = { kind: 'session', id: 5 };
    const w = mountShell();
    expect(w.find('.phone-top').exists()).toBe(false);
    expect((w.get('.phone-tabs').element as HTMLElement).style.display).not.toBe('none');
    await w.get('.stub-field').trigger('focusin');
    expect((w.get('.phone-tabs').element as HTMLElement).style.display).toBe('none');
    // тап по кнопке рядом с полем фокус держит — вкладки не прыгают обратно
    await w.get('.stub-field').trigger('focusout', { relatedTarget: w.get('.stub-btn').element });
    expect((w.get('.phone-tabs').element as HTMLElement).style.display).toBe('none');
    await w.get('.stub-field').trigger('focusout', { relatedTarget: null });
    expect((w.get('.phone-tabs').element as HTMLElement).style.display).not.toBe('none');
    w.unmount();
    store.state.phoneSession = null;
  });

  it('вне сессии и беседы (список) фокус в поле вкладки не прячет; полоса раздела на месте', async () => {
    store.state.phoneTab = 'orch';
    store.state.phoneSession = null;
    const w = mountShell();
    expect(w.find('.phone-top').exists()).toBe(true);
    await w.get('.stub-field').trigger('focusin');
    expect((w.get('.phone-tabs').element as HTMLElement).style.display).not.toBe('none');
    w.unmount();
  });
});
