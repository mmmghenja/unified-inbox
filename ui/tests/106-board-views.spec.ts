// Ф47-1 (`docs/architecture.md`): виды «Главной» — переключатель, «По проектам» (столбец на проект,
// строка сессий, выбор), «Один проект» (все сессии столбцами), клики в списке слева, беседа окном поверх.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HomeReactItem, HubSession, Project, ProjectSub } from '../src/api/types';
import BoardView from '../src/components/BoardView.vue';
import { boardState, normalizeLayout, openColumn, resetBoard, setView } from '../src/store/board';
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


function twoProjects(): void {
  store.state.projects = [project(), project({ id: 4, name: 'Globex', color: '#00ff00' }), project({ id: 6, name: 'Пустой' })];
  store.state.sessions.items = {
    5: session(),
    6: session({ id: 6, role: 'тестер', state: 'running' }),
    7: session({ id: 7, project_id: 4, role: 'дежурный', pending_requests: 1 }),
  };
}

async function mountView(view: 'own' | 'projects' | 'project'): Promise<ReturnType<typeof mount>> {
  boardState.layout.view = view;
  const w = mount(BoardView, { attachTo: document.body });
  await flushPromises();
  return w;
}

describe('Ф47-1: раскладка и переключатель', () => {
  it('нормализация: вид, проект и выбор сессий; мусор и чужие виды выбрасываются', () => {
    expect(normalizeLayout({}).view).toBe('own');
    expect(normalizeLayout({ view: 'grid' }).view).toBe('own');
    const n = normalizeLayout({
      view: 'project',
      view_project: 3,
      picks: { '3': { kind: 'session', id: 5 }, '4': { kind: 'channel', id: 1 }, x: { kind: 'session', id: 2 }, '5': { kind: 'orch' } },
    });
    expect(n.view).toBe('project');
    expect(n.view_project).toBe(3);
    expect(n.picks).toEqual({ '3': { kind: 'session', id: 5 } });
    expect(normalizeLayout({ view_project: 0 }).view_project).toBeNull();
  });

  it('кнопки вида: сохраняют вид в раскладке и не трогают свой набор столбцов', async () => {
    twoProjects();
    openColumn({ kind: 'session', id: 5 });
    const w = await mountView('own');
    await w.get('.board-toolbar-viewbtn').trigger('click'); // Ф90-2: вид — в выпадашке «Вид ▾»
    expect(w.get('[data-view="own"]').attributes('aria-pressed')).toBe('true');
    await w.get('[data-view="projects"]').trigger('click');
    expect(boardState.layout.view).toBe('projects');
    expect(w.get('[data-view="projects"]').attributes('aria-pressed')).toBe('true');
    expect(w.get('[data-view="own"]').attributes('aria-pressed')).toBe('false');
    await w.get('[data-view="project"]').trigger('click');
    expect(boardState.layout.view).toBe('project');
    await w.get('[data-view="own"]').trigger('click');
    expect(boardState.layout.open).toEqual([{ kind: 'session', id: 5 }]);
    expect(w.find('.board-columns > [data-col-key="session:5"]').exists()).toBe(true);
    w.unmount();
  });

  it('смена вида уходит в ядро (PUT /ui/layout) с ключами вида', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const put = vi.fn(async () => json({ layout: {} }));
    vi.stubGlobal('fetch', put);
    setView('projects');
    vi.advanceTimersByTime(1000);
    expect(put).toHaveBeenCalled();
    const body = JSON.parse(String((put.mock.calls[0] as unknown as [string, RequestInit])[1].body));
    expect(body.layout.view).toBe('projects');
    vi.useRealTimers();
  });
});

describe('Ф47-1: вид «По проектам»', () => {
  it('столбец на проект с сессиями, пустой проект и скрытый не показываются, в шапке «ждёт тебя · N»', async () => {
    twoProjects();
    const w = await mountView('projects');
    const cols = w.findAll('.board-project');
    expect(cols.map((c) => c.attributes('data-col-key'))).toEqual(['project:3', 'project:4']);
    expect(cols[1]!.get('.board-project-wait').text()).toBe('ждёт тебя · 1');
    expect(cols[0]!.find('.board-project-wait').exists()).toBe(false);
    await cols[1]!.get('.board-project-hide').trigger('click');
    expect(boardState.layout.hidden_projects).toEqual([4]);
    expect(w.findAll('.board-project')).toHaveLength(1);
    w.unmount();
  });

  it('строка всех сессий проекта кнопками; по умолчанию — ждущая, потом работающая, потом первая', async () => {
    twoProjects();
    const w = await mountView('projects');
    const first = w.get('[data-col-key="project:3"]');
    expect(first.findAll('[data-pick-key]').map((b) => b.attributes('data-pick-key'))).toEqual(['session:5', 'session:6']);
    // работающий тестер раньше простаивающего программиста
    expect(first.get('[data-pick-key="session:6"]').attributes('aria-pressed')).toBe('true');
    expect(first.get('[data-pick-key="session:5"]').attributes('aria-pressed')).toBe('false');
    expect(first.get('[data-pick-key="session:6"]').text()).toContain('работает');
    expect(first.find('.board-col[data-col-key="session:6"]').exists()).toBe(true);
    expect(first.find('.board-col-close').exists()).toBe(false);
    expect(first.find('.board-col-left').exists()).toBe(false);
    // у проекта с ждущей сессией — она
    expect(w.get('[data-col-key="project:4"] [data-pick-key="session:7"]').attributes('aria-pressed')).toBe('true');
    w.unmount();
  });

  it('клик по кнопке сессии выбирает её и сохраняет выбор; пропавший выбор откатывается к умолчанию', async () => {
    twoProjects();
    const w = await mountView('projects');
    await w.get('[data-col-key="project:3"] [data-pick-key="session:5"]').trigger('click');
    await flushPromises();
    expect(boardState.layout.picks['3']).toEqual({ kind: 'session', id: 5 });
    const col = w.get('[data-col-key="project:3"]');
    expect(col.get('[data-pick-key="session:5"]').attributes('aria-pressed')).toBe('true');
    expect(col.find('.board-col[data-col-key="session:5"]').exists()).toBe(true);
    expect(col.find('.board-col[data-col-key="session:6"]').exists()).toBe(false);
    boardState.layout.picks['3'] = { kind: 'session', id: 99 };
    await flushPromises();
    expect(w.get('[data-col-key="project:3"] [data-pick-key="session:6"]').attributes('aria-pressed')).toBe('true');
    w.unmount();
  });

  it('внешняя линия — тоже кнопка; «+» открывает форму новой сессии в этом проекте', async () => {
    twoProjects();
    store.state.peerMsgs.agentSubs = [sub()];
    const w = await mountView('projects');
    expect(w.find('[data-col-key="project:3"] [data-pick-key="agent:p3:маркетолог:1"]').exists()).toBe(true);
    expect(w.findComponent({ name: 'NewSessionForm' }).exists()).toBe(false);
    await w.get('[data-col-key="project:3"] .board-pick-new').trigger('click');
    expect(w.get('[data-col-key="project:3"] .board-pick-new').attributes('aria-expanded')).toBe('true');
    expect(w.get('[data-col-key="project:3"]').findComponent({ name: 'NewSessionForm' }).props('projectId')).toBe(3);
    w.unmount();
  });

  it('«ждёт тебя · N» раскрывает сводку на этом проекте', async () => {
    twoProjects();
    boardState.layout.wait_open = false;
    const w = await mountView('projects');
    await w.get('[data-col-key="project:4"] .board-project-wait').trigger('click');
    expect(boardState.layout.wait_open).toBe(true);
    expect(boardState.waitProject).toBe(4);
    w.unmount();
  });

  it('сессий нет — подсказка', async () => {
    store.state.projects = [project()];
    const w = await mountView('projects');
    expect(w.get('.board-columns-empty').text()).toContain('Сессий пока нет');
    w.unmount();
  });
});

describe('Ф47-1: вид «Один проект»', () => {
  it('по умолчанию — первый проект, где что-то ждёт; все его сессии столбцами и «+ Новая сессия»', async () => {
    twoProjects();
    const w = await mountView('project');
    const bar = w.get('.board-projbar');
    expect(bar.findAll('.board-projbar-btn').map((b) => b.attributes('data-project-id'))).toEqual(['3', '4']);
    expect(bar.get('[data-project-id="4"]').attributes('aria-pressed')).toBe('true');
    expect(bar.get('[data-project-id="4"] .badge').text()).toBe('1');
    expect(w.findAll('.board-columns .board-col').map((c) => c.attributes('data-col-key'))).toEqual(['session:7']);
    expect(w.get('.board-new-col-btn').text()).toBe('+ Новая сессия в Globex');
    expect(w.find('.board-col-close').exists()).toBe(false);
    w.unmount();
  });

  it('смена проекта показывает все его сессии и сохраняется; удалённый проект — к умолчанию', async () => {
    twoProjects();
    const w = await mountView('project');
    await w.get('.board-projbar [data-project-id="3"]').trigger('click');
    expect(boardState.layout.view_project).toBe(3);
    expect(w.findAll('.board-columns .board-col').map((c) => c.attributes('data-col-key'))).toEqual(['session:5', 'session:6']);
    boardState.layout.view_project = 55;
    await flushPromises();
    expect(w.get('.board-projbar [data-project-id="4"]').attributes('aria-pressed')).toBe('true');
    w.unmount();
  });

  it('ничего не ждёт — первый проект; «+» открывает форму в нём', async () => {
    twoProjects();
    store.state.sessions.items = { 5: session() };
    const w = await mountView('project');
    expect(w.get('.board-projbar [data-project-id="3"]').attributes('aria-pressed')).toBe('true');
    await w.get('.board-new-col-btn').trigger('click');
    expect(w.findComponent({ name: 'NewSessionForm' }).props('projectId')).toBe(3);
    w.unmount();
  });
});

describe('Ф47-1: список слева в новых видах', () => {
  it('«По проектам»: клик по сессии выбирает её в столбце проекта и подсвечивает столбец', async () => {
    twoProjects();
    const w = await mountView('projects');
    await w.get('.board-list [data-session-id="5"]').trigger('click');
    await flushPromises();
    expect(boardState.layout.picks['3']).toEqual({ kind: 'session', id: 5 });
    expect(boardState.layout.open).toEqual([]);
    expect(boardState.focusKey).toBe('project:3');
    w.unmount();
  });

  it('«Один проект»: клик по сессии другого проекта переключает проект и прокручивает к её столбцу', async () => {
    twoProjects();
    const w = await mountView('project');
    await w.get('.board-list [data-session-id="5"]').trigger('click');
    await flushPromises();
    expect(boardState.layout.view_project).toBe(3);
    expect(boardState.layout.open).toEqual([]);
    expect(boardState.focusKey).toBe('session:5');
    w.unmount();
  });

  it('беседа открывается окном поверх, а не столбцом', async () => {
    twoProjects();
    store.state.home = {
      ...store.state.home,
      react: [
        {
          channel: { id: 77, title: 'Чат' } as HomeReactItem['channel'],
          project: { id: 3, name: null, color: null },
          last: { sender: null, excerpt: null, ts: NOW, message_id: 1 },
          card: null,
          draft_count: 0,
        },
      ],
    };
    // опрос главной при монтировании не должен затереть заданную беседу
    vi.spyOn(store, 'loadHome').mockResolvedValue(undefined);
    const overlay = vi.spyOn(store, 'openChannelOverlay').mockResolvedValue(undefined);
    window.localStorage.setItem('hub.board.chats.3', '1'); // «беседы ждут» раскрыты
    for (const view of ['projects', 'project'] as const) {
      const w = await mountView(view);
      await w.get('.board-list [data-channel-id="77"]').trigger('click');
      w.unmount();
    }
    expect(overlay).toHaveBeenCalledTimes(2);
    expect(overlay).toHaveBeenCalledWith(77);
    expect(boardState.layout.open).toEqual([]);
  });

  it('оркестратор возвращает на «Свой набор» и открывает столбцом', async () => {
    twoProjects();
    const w = await mountView('projects');
    await w.get('.board-row--owner').trigger('click');
    expect(boardState.layout.view).toBe('own');
    expect(boardState.layout.open).toEqual([{ kind: 'owner_channel' }]);
    w.unmount();
  });
});

describe('Ф48-2 E: «Ждёт тебя» в столбце проекта', () => {
  it('«По проектам»: блок только у проекта с делами, свёрнутость помнится', async () => {
    window.localStorage.removeItem('hub.board_wait_closed.4');
    twoProjects();
    const w = await mountView('projects');
    expect(w.find('[data-col-key="project:3"] .board-wait').exists()).toBe(false);
    const block = w.get('[data-col-key="project:4"] .board-wait');
    expect(block.get('.board-wait-head').text()).toContain('Ждёт тебя · 1');
    expect(block.find('.board-wait-body').exists()).toBe(true);
    await block.get('.board-wait-head').trigger('click');
    expect(w.get('[data-col-key="project:4"] .board-wait').find('.board-wait-body').exists()).toBe(false);
    expect(window.localStorage.getItem('hub.board_wait_closed.4')).toBe('1');
    w.unmount();
    const again = await mountView('projects');
    expect(again.get('[data-col-key="project:4"] .board-wait').find('.board-wait-body').exists()).toBe(false);
    again.unmount();
    window.localStorage.removeItem('hub.board_wait_closed.4');
  });

  it('«Один проект»: блок у выбранного проекта с делами; «Свой набор» — без блока', async () => {
    window.localStorage.removeItem('hub.board_wait_closed.4');
    twoProjects();
    boardState.layout.view_project = 4;
    const w = await mountView('project');
    expect(w.find('.board-oneproject .board-wait').exists()).toBe(true);
    boardState.layout.view_project = 3;
    await flushPromises();
    expect(w.find('.board-oneproject .board-wait').exists()).toBe(false);
    await w.get('.board-toolbar-viewbtn').trigger('click');
    await w.get('[data-view="own"]').trigger('click');
    expect(w.find('.board-wait').exists()).toBe(false);
    w.unmount();
  });
});
