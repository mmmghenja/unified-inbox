// Ф41-6б (`docs/architecture.md` и 4): телефон — вкладка «Сессии» (`PhoneSessions`) вместо
// «Оркестратор»: список по проектам, оркестратор, внешние «как агенту», тап — на весь экран с «Назад» общим механизмом
// уровней; «Переписка агентов» и «Задачи» ведут в «Диспетчерскую»; «Диспетчерская» не монтирует чат оркестратора и блок
// сессий.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, ProjectSub } from '../src/api/types';
import ClaudeView from '../src/components/ClaudeView.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import { boardState, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';
import { initLayout } from '../src/util/layout';
import { fakeMedia } from './media';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function stubFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      const p = url.pathname;
      if (p === '/api/v1/subs' && url.searchParams.get('offline') === '1') return json({ lines: [] });
      if (p === '/api/v1/subs') return json({ items: store.state.peerMsgs.agentSubs, channels: [] });
      if (p === '/api/v1/sessions') return json({ items: Object.values(store.state.sessions.items) });
      if (/^\/api\/v1\/sessions\/\d+$/.test(p)) {
        const id = Number(p.split('/').pop());
        return json({ session: store.state.sessions.items[id] ?? session({ id }), requests: [], says: [] });
      }
      if (/\/events$/.test(p)) return json([]);
      return json([]);
    }),
  );
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true, ...overrides };
}

function session(overrides: Partial<HubSession> = {}): HubSession {
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
    state: 'idle',
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
    listening: false,
    engine_session_id: null,
    workdir: '/p',
    cache: null,
    usage: null,
    ...overrides,
  };
}

function sub(overrides: Partial<ProjectSub> = {}): ProjectSub {
  return {
    id: 40,
    project_id: 3,
    role: 'маркетолог',
    role_no: 1,
    mode: 'messages',
    engine: 'claude',
    session_name: null,
    live: true,
    listening: true,
    waiting: false,
    ...overrides,
  };
}

let pushState: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  initLayout(fakeMedia(390).matchMedia);
  stubFetch();
  pushState = vi.spyOn(window.history, 'pushState').mockImplementation(() => undefined);
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project()];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
  store.state.sessions.items = { 5: session({ pending_requests: 1 }) };
  store.state.peerMsgs.agentSubs = [sub()];
  store.state.phoneTab = 'orch';
  store.state.phoneSession = null;
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
});

afterEach(() => {
  store.state.phoneTab = 'wait';
  store.state.phoneSession = null;
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  initLayout(null);
  resetBoard();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-6б: телефон — вкладка «Сессии»', () => {
  it('четвёртая вкладка называется «Сессии»; список: «Ждёт тебя», сессии и внешние «как агенту»', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    expect(w.get('.phone-tab--orch').text()).toBe('Сессии');
    expect(w.get('.phone-title').text()).toBe('Сессии');
    expect(w.get('.phone-sessions-head').text()).toContain('Новая');
    expect(w.get('.phone-sessions-wait').text()).toContain('Ждёт тебя · 1');
    expect(w.find('.board-row--orch').exists()).toBe(false); // чат оркестратора из интерфейса убран (Ф56-3)
    expect(w.get('[data-session-id="5"]').text()).toContain('программист');
    expect(w.get('[data-session-id="5"]').text()).toContain('ждёт тебя');
    expect(w.get('[data-agent-key]').text()).toContain('как агенту');
    w.unmount();
  });

  it('«Ждёт тебя · N» ведёт на вкладку «Ждёт»', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    await w.get('.phone-sessions-wait').trigger('click');
    expect(store.state.phoneTab).toBe('wait');
    w.unmount();
  });

  it('тап по сессии — окно на весь экран (одна запись истории), «Назад» возвращает к списку', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    await w.get('[data-session-id="5"]').trigger('click');
    await flushPromises();
    expect(store.state.phoneSession).toEqual({ kind: 'session', id: 5 });
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(pushState.mock.calls[0]![0]).toMatchObject({ hubPhone: 'session' });
    expect(w.find('.phone-session-full .session-window').exists()).toBe(true);
    expect(w.find('main.phone-sessions').exists()).toBe(false);
    await w.get('.phone-session-head .back-button').trigger('click');
    await flushPromises();
    expect(store.state.phoneSession).toBeNull();
    expect(w.find('main.phone-sessions').exists()).toBe(true);
    w.unmount();
  });

  it('тап по внешней сессии — диалог агента на весь экран', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    await w.get('[data-agent-key]').trigger('click');
    await flushPromises();
    expect(store.state.phoneSession?.kind).toBe('agent');
    expect(w.find('.phone-session-full').exists()).toBe(true);
    expect(w.get('.phone-session-title').text()).toBe('маркетолог');
    w.unmount();
  });

  it('смена вкладки закрывает полноэкранный уровень', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    await w.get('[data-session-id="5"]').trigger('click');
    store.setPhoneTab('wait');
    expect(store.state.phoneSession).toBeNull();
    w.unmount();
  });

  it('«Переписка агентов» и «Задачи» открывают страницу «Диспетчерская»', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    await w.get('.phone-sessions-traffic').trigger('click');
    await flushPromises();
    expect(store.state.phoneTab).toBe('dispatch');
    expect(w.get('.phone-title').text()).toBe('Агенты');
    expect(w.find('main.claude-view').exists()).toBe(true);
    w.unmount();
  });

  it('переход «к сессии» на телефоне — вкладка «Сессии», сессия на весь экран', async () => {
    store.state.phoneTab = 'wait';
    store.revealSession(3, 5);
    expect(store.state.phoneTab).toBe('orch');
    expect(store.state.phoneSession).toEqual({ kind: 'session', id: 5 });
  });

  it('раскладка доски на телефоне не используется: столбцы не открываются', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    await w.get('[data-session-id="5"]').trigger('click');
    expect(boardState.layout.open).toEqual([]);
    w.unmount();
  });
});

describe('Ф41-6б: «Диспетчерская» без чата оркестратора и блока сессий', () => {
  it('ClaudeView (телефон и компьютер) не монтирует OrchestratorChat и SessionsBlock', async () => {
    for (const phone of [true, false]) {
      const w = mount(ClaudeView, { props: { phone } });
      await flushPromises();
      expect(w.find('[data-section="sessions"]').exists()).toBe(false);
      w.unmount();
    }
  });
});

describe('телефон: убранные «×» линии «не в сети» не возвращаются (раскладка из ядра)', () => {
  function offline(key: string, role: string, ended: number) {
    return {
      key, project_id: 3, role, role_no: 1, engine: 'claude', session_name: null, cwd: null, ended_ts: ended,
      end_reason: null, pending: 0, sub_id: 1,
    };
  }

  function stubWithLayout(hidden: { key: string; ended_ts: number }[], puts: unknown[]): void {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        const url = new URL(input, 'http://localhost');
        const p = url.pathname;
        if (p === '/api/v1/ui/layout') {
          if (init?.method === 'PUT') {
            puts.push(JSON.parse(String(init.body)));
            return json({ layout: {} });
          }
          return json({ layout: { v: 1, hidden_lines: hidden, open: [{ kind: 'session', id: 9 }] } });
        }
        if (p === '/api/v1/subs' && url.searchParams.get('offline') === '1') {
          return json({ lines: [offline('a:old', 'стратег', 100), offline('a:new', 'сетевик', 100)] });
        }
        if (p === '/api/v1/subs') return json({ items: [], channels: [] });
        if (p === '/api/v1/sessions') return json({ items: [] });
        return json([]);
      }),
    );
  }

  it('скрытая на ноутбуке линия не видна; «×» на телефоне сохраняет раскладку целиком', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const puts: unknown[] = [];
    stubWithLayout([{ key: 'a:old', ended_ts: 100 }], puts);
    store.state.sessions.items = {};
    store.state.peerMsgs.agentSubs = [];
    const w = mount(PhoneShell);
    await flushPromises();
    expect(w.find('[data-agent-key="a:old"]').exists()).toBe(false);
    expect(w.find('[data-agent-key="a:new"]').exists()).toBe(true);

    await w.get('[data-agent-key="a:new"] + .board-row-remove').trigger('click');
    await flushPromises();
    vi.advanceTimersByTime(1000);
    await flushPromises();
    expect(w.find('[data-agent-key="a:new"]').exists()).toBe(false);
    const saved = (puts.at(-1) as { layout: { hidden_lines: { key: string }[]; open: unknown[] } }).layout;
    expect(saved.hidden_lines.map((h) => h.key)).toEqual(['a:old', 'a:new']);
    expect(saved.open).toEqual([{ kind: 'session', id: 9 }]); // раскладка ноутбука не затёрта
    vi.useRealTimers();
    w.unmount();
  });
});
