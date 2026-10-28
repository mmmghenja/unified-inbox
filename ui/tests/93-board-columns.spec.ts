// Ф41-6a (`docs/architecture.md`): столбцы доски — сессия хаба (окно в режиме столбца),
// внешняя сессия (диалог «как агенту», «Забрать в хаб» неактивна), оркестратор; шапка (×, «Левее/Правее»), число
// столбцов, повторное открытие подсвечивает, переходы «к сессии» ведут на «Главную» со столбцом, «Диспетчерская»
// вместо вкладки «Claude».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, ProjectSub } from '../src/api/types';
import BoardColumns from '../src/components/BoardColumns.vue';
import BoardView from '../src/components/BoardView.vue';
import ClaudeView from '../src/components/ClaudeView.vue';
import MainApp from '../src/components/MainApp.vue';
import { boardState, openColumn, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let layoutAnswer: () => Response = () => json({ detail: 'not found' }, 404);

function stubFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/ui/layout') return layoutAnswer();
      if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      if (p === '/api/v1/sessions') return json({ items: Object.values(store.state.sessions.items) });
      if (p === '/api/v1/subs') return json({ items: store.state.peerMsgs.agentSubs, channels: [] });
      if (/\/events$/.test(p)) return json([]);
      if (/^\/api\/v1\/sessions\/\d+$/.test(p)) {
        const id = Number(p.split('/').pop());
        const found = store.state.sessions.items[id] ?? session({ id });
        const requests =
          found.pending_requests > 0
            ? [{ id: 100, session_id: id, project_id: found.project_id, kind: 'permission', tool: 'Bash', input: {}, input_truncated: false, reason: null, status: 'pending', answer: null, created_ts: NOW, decided_ts: null, delivered: false, seq: 11 }]
            : [];
        return json({ session: found, requests, says: [] });
      }
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

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  layoutAnswer = () => json({ detail: 'not found' }, 404);
  stubFetch();
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project()];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
});

afterEach(() => {
  resetBoard();
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-6a: BoardColumns', () => {
  it('пусто — подсказка в области столбцов', () => {
    const w = mount(BoardColumns);
    expect(w.get('.board-columns-empty').text()).toBe('Выбери слева сессию или беседу — она откроется здесь столбцом');
  });

  it('столбец сессии: шапка «Проект / имя», чип, окно в режиме столбца; «×» убирает столбец, не сессию', async () => {
    store.state.sessions.items = { 5: session({ pending_requests: 1 }) };
    openColumn({ kind: 'session', id: 5 });
    const w = mount(BoardColumns);
    await flushPromises();
    const col = w.get('[data-col-key="session:5"]');
    // Ф90-1: шапка — одна строка: точка состояния (подпись в title), имя; проект — в «Сведениях» меню «⋯».
    expect(col.find('.board-col-project').exists()).toBe(false);
    expect(col.get('.board-col-name').text()).toBe('программист');
    expect(col.get('.board-col-dot').attributes('title')).toBe('ждёт тебя');
    await col.get('.board-col-menu').trigger('click');
    expect(col.get('.session-menu-info').text()).toContain('Umbrella');
    await col.get('.board-col-menu').trigger('click');
    expect(col.get('.session-window').classes()).toContain('session-window--column');
    expect(col.find('textarea').exists()).toBe(true);
    // пока столбец смонтирован, окно считается раскрытым (стор дочитывает события)
    expect(store.state.sessions.expanded[5]).toBe(true);
    await col.get('.board-col-close').trigger('click');
    expect(boardState.layout.open).toEqual([]);
    expect(store.state.sessions.items[5]).toBeDefined();
    expect(store.state.sessions.expanded[5]).toBe(false);
    w.unmount();
  });

  it('чип: работает / спит / причина ожидания', async () => {
    store.state.sessions.items = { 5: session({ state: 'running' }), 6: session({ id: 6, state: 'asleep' }), 7: session({ id: 7, wait_reason: 'slot' }) };
    for (const id of [5, 6, 7]) openColumn({ kind: 'session', id });
    const w = mount(BoardColumns);
    await flushPromises();
    const chips = w.findAll('.board-col-dot').map((c) => c.attributes('title'));
    expect(chips[0]).toBe('работает');
    expect(chips[1]).toBe('спит');
    expect(chips[2]).toBe('ждёт места');
    w.unmount();
  });

  it('«Левее»/«Правее» меняют порядок', async () => {
    store.state.sessions.items = { 5: session(), 6: session({ id: 6, role: 'тестер' }) };
    openColumn({ kind: 'session', id: 5 });
    openColumn({ kind: 'session', id: 6 });
    const w = mount(BoardColumns);
    await flushPromises();
    await w.get('[data-col-key="session:6"] .board-col-menu').trigger('click');
    await w.get('[data-col-key="session:6"] .board-col-left').trigger('click');
    expect(boardState.layout.open.map((o) => o.id)).toEqual([6, 5]);
    await flushPromises();
    expect(w.findAll('.board-col').map((c) => c.attributes('data-col-key'))).toEqual(['session:6', 'session:5']);
    w.unmount();
  });

  it('число столбцов — переменная ширины; повторное открытие подсвечивает и прокручивает к столбцу', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    store.state.sessions.items = { 5: session() };
    openColumn({ kind: 'session', id: 5 });
    boardState.layout.columns = 4;
    const w = mount(BoardColumns, { attachTo: document.body });
    await flushPromises();
    expect(w.get('.board-columns').attributes('style')).toContain('--board-n: 4');
    const el = w.get('[data-col-key="session:5"]').element as HTMLElement;
    const scroll = vi.fn();
    el.scrollIntoView = scroll;
    openColumn({ kind: 'session', id: 5 });
    await flushPromises();
    expect(boardState.layout.open).toHaveLength(1);
    expect(scroll).toHaveBeenCalled();
    expect(el.classList.contains('flash')).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(el.classList.contains('flash')).toBe(false);
    w.unmount();
    vi.useRealTimers();
  });

  it('столбец закрытой сессии — пустышка с «×»', () => {
    openColumn({ kind: 'session', id: 99 });
    const w = mount(BoardColumns);
    expect(w.get('.board-empty').text()).toContain('Сессия закрыта');
    expect(w.find('.board-col-close').exists()).toBe(true);
  });

  it('внешняя сессия: пояснение «как агенту», диалог агента', async () => {
    store.state.peerMsgs.agentSubs = [sub()];
    openColumn({ kind: 'agent', key: 'p3:маркетолог:1' });
    const w = mount(BoardColumns);
    await flushPromises();
    const col = w.get('[data-col-key="agent:p3:маркетолог:1"]');
    expect(col.get('.board-agent-note').text()).toContain('писать ей можно как агенту');
    expect(col.get('.board-col-chip').text()).toBe('слушает');
    expect(col.find('.agent-dialog, textarea').exists()).toBe(true);
    w.unmount();
  });

  it('линия пропала — «не в сети», без диалога', async () => {
    openColumn({ kind: 'agent', key: 'p3:ушёл:1' });
    const w = mount(BoardColumns);
    expect(w.get('.board-empty').text()).toContain('не в сети');
    w.unmount();
  });

  it('«Агент»: один столбец с очередью, чата оркестратора нет (Ф56-3)', async () => {
    openColumn({ kind: 'owner_channel' });
    const w = mount(BoardColumns);
    await flushPromises();
    expect(w.findAll('section.agent-queue')).toHaveLength(1);
    w.unmount();
  });
});

describe('Ф41-6a: BoardView и переходы', () => {
  it('ответ ядра без ручки (404) — раскладка в памяти; столбец закрытой сессии убирается после загрузки списка', async () => {
    store.state.sessions.items = { 5: session() };
    openColumn({ kind: 'session', id: 5 });
    openColumn({ kind: 'session', id: 77 });
    const w = mount(BoardView);
    await flushPromises();
    expect(w.find('.wait-bar').exists()).toBe(true);
    expect(w.find('.board-toolbar').exists()).toBe(true);
    expect(w.find('.board-list').exists()).toBe(true);
    expect(boardState.loaded).toBe(true);
    expect(boardState.layout.open).toEqual([{ kind: 'session', id: 5 }]);
    w.unmount();
  });

  it('раскладка из ядра применяется: столбцы, число, свёрнутый список', async () => {
    store.state.sessions.items = { 5: session() };
    layoutAnswer = () =>
      json({ layout: { v: 1, list_collapsed: true, columns: 2, wait_open: false, open: [{ kind: 'session', id: 5 }], hidden_projects: [] } });
    const w = mount(BoardView);
    await flushPromises();
    expect(w.find('.board-list--collapsed').exists()).toBe(true);
    expect(w.find('[data-col-key="session:5"]').exists()).toBe(true);
    expect(w.find('.wait-bar-panel').exists()).toBe(false);
    w.unmount();
  });

  it('«к сессии» на компьютере ведёт на «Главную» со столбцом сессии; задачи по «Го» — в «Диспетчерскую»', () => {
    store.state.homeOpen = false;
    store.state.claudeOpen = true;
    store.state.sessions.items = { 5: session() };
    store.revealSession(3, 5);
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.claudeOpen).toBe(false);
    expect(boardState.layout.open).toEqual([{ kind: 'session', id: 5 }]);
    store.revealProject(3);
    expect(store.state.claudeOpen).toBe(true);
    expect(store.state.homeOpen).toBe(false);
  });

  it('MainApp: на «Главной» — доска; «Диспетчерская» — прежняя страница без чата оркестратора и без блока сессий', async () => {
    store.state.projects = [project()];
    const app = mount(MainApp);
    await flushPromises();
    expect(app.find('.board-view').exists()).toBe(true);
    expect(app.find('.project-tab--claude').exists()).toBe(false);
    app.unmount();
    const view = mount(ClaudeView);
    await flushPromises();
    expect(view.attributes('aria-label')).toBe('Агенты');
    expect(view.find('.session-block').exists()).toBe(false);
    expect(view.find('.agents-block').exists()).toBe(true);
    view.unmount();
  });
});
