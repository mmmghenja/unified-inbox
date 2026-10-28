// Ф41-6a (`docs/architecture.md`): список слева (сессии хаба, живые внешние, «беседы ждут»,
// «Неразобранное», скрытые проекты, свёрнутый вид), полоса «Ждёт тебя · N» (число совпадает с вкладкой «Ждёт»
// телефона), полоса управления.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HomeReactItem, HubSession, OutboxListItem, Project, ProjectSub, SessionRequest } from '../src/api/types';
import BoardList from '../src/components/BoardList.vue';
import BoardToolbar from '../src/components/BoardToolbar.vue';
import TopBar from '../src/components/TopBar.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import WaitBar from '../src/components/WaitBar.vue';
import { boardState, resetBoard, toggleWait } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';
import { buildBoardList, sessionRow, waitSummary, workSummary } from '../src/util/boardList';
import { buildAgentLines } from '../src/store/peerMsgs';
import { buildWaiting } from '../src/util/waiting';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function stubFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/ui/layout') return json({ detail: 'not found' }, 404);
      if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      // списки «слежения» (сессии, агенты, домашняя) в этих тестах не меняют стор: отдаём то, что в нём лежит
      if (p === '/api/v1/sessions') return json({ items: Object.values(store.state.sessions.items) });
      if (p === '/api/v1/subs') return json({ items: store.state.peerMsgs.agentSubs, channels: [] });
      return json([]);
    }),
  );
}

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 3,
    name: 'Umbrella',
    color: '#ff0000',
    sort: 0,
    muted: false,
    workdir: '/p',
    unread: 0,
    subs: true,
    ...overrides,
  };
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
    ...overrides,
  };
}

function sreq(overrides: Partial<SessionRequest> = {}): SessionRequest {
  return {
    id: 100,
    session_id: 5,
    project_id: 3,
    kind: 'permission',
    tool: 'Bash',
    input: {},
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

function reactItem(channelId: number, projectId: number, title: string, draftCount = 0): HomeReactItem {
  return {
    channel: { id: channelId, title } as HomeReactItem['channel'],
    project: { id: projectId, name: null, color: null },
    last: { sender: null, excerpt: null, ts: NOW, message_id: 1 },
    card: null,
    draft_count: draftCount,
  };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  window.localStorage.clear();
  stubFetch();
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project(), project({ id: 4, name: 'Личное', color: '#00ff00', workdir: null })];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
});

afterEach(() => {
  resetBoard();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-6a: строка сессии', () => {
  it('точка и подпись: ждёт тебя / спрашивает / работает / спит / ждёт входа; авто и кэш', () => {
    expect(sessionRow(session({ pending_requests: 1 }), undefined, NOW)).toMatchObject({ dot: 'wait', sub: 'ждёт тебя' });
    expect(sessionRow(session(), [sreq({ kind: 'question' })], NOW)).toMatchObject({ dot: 'wait', sub: 'спрашивает' });
    expect(sessionRow(session({ wait_reason: 'heavy' }), undefined, NOW)).toMatchObject({ dot: 'wait' });
    expect(sessionRow(session({ wait_reason: 'paused' }), undefined, NOW)).toMatchObject({ dot: 'wait' });
    expect(sessionRow(session({ state: 'running' }), undefined, NOW)).toMatchObject({ dot: 'run', sub: 'работает' });
    expect(sessionRow(session({ state: 'asleep' }), undefined, NOW)).toMatchObject({ dot: 'idle', sub: 'спит' });
    expect(sessionRow(session({ state: 'idle' }), undefined, NOW)).toMatchObject({ dot: 'idle', sub: 'ждёт входа' });
    expect(sessionRow(session({ mode: 'auto' as never }), undefined, NOW).auto).toBe(true);
    const cache = { last_ts: NOW, ttl_s: 3600, expires_ts: NOW + 6 * 60_000 + 12_000, context_tokens: 120_000, model: 'x' };
    expect(sessionRow(session({ cache }), undefined, NOW).cacheWarn).toBe('кэш 06:12');
    expect(sessionRow(session({ cache: { ...cache, context_tokens: 50_000 } }), undefined, NOW).cacheWarn).toBeNull();
    expect(sessionRow(session({ cache: { ...cache, expires_ts: NOW + 30 * 60_000 } }), undefined, NOW).cacheWarn).toBeNull();
  });

  it('Ф98-1: вторая строка — движок, модель и усилие; без модели — «модель по умолчанию»', () => {
    expect(sessionRow(session({ model: 'sonnet', effort: 'high' }), undefined, NOW).engineModel).toBe(
      'Claude Code · sonnet · high',
    );
    expect(sessionRow(session(), undefined, NOW).engineModel).toBe('Claude Code · модель по умолчанию');
    expect(
      sessionRow(session({ engine: 'opencode', model: 'deepseek/deepseek-flash', effort: 'max' }), undefined, NOW)
        .engineModel,
    ).toBe('OpenCode · deepseek/deepseek-flash · max');
  });
});

describe('Ф41-6a: состав списка', () => {
  function build(overrides: Partial<Parameters<typeof buildBoardList>[0]> = {}) {
    return buildBoardList({
      projects: store.state.projects,
      sessions: [],
      pending: {},
      agentLines: [],
      home: { react: [], drafts: [] },
      waiting: [],
      hiddenProjects: [],
      nowMs: NOW,
      ...overrides,
    });
  }

  it('проект с рабочей папкой есть всегда, без папки и без ничего — нет; сессии закрытые не входят', () => {
    const groups = build({ sessions: [session(), session({ id: 6, closed_ts: NOW })] });
    expect(groups.map((g) => g.id)).toEqual([3]);
    expect(groups[0]!.sessions.map((s) => s.id)).toEqual([5]);
  });

  it('внешние живые: только не-хабовские и живые; «беседы ждут» без повторов (реакция и черновик), «Неразобранное» последним', () => {
    const lines = buildAgentLines({}, [
      sub(),
      sub({ id: 41, role: 'исследователь', held: true }),
      sub({ id: 42, role: 'тестер', live: false }),
    ]);
    const draft = { id: 9, channel_id: 21, project_id: 3, channel_title: 'Руководители ПВЗ' } as OutboxListItem;
    const groups = build({
      agentLines: lines,
      home: {
        react: [reactItem(21, 3, 'Руководители ПВЗ', 1), reactItem(22, 3, 'Личка'), reactItem(30, -1, 'Банк')],
        drafts: [draft],
      },
    });
    expect(groups.map((g) => g.id)).toEqual([3, -1]);
    expect(groups[0]!.agents.map((a) => a.name)).toEqual(['маркетолог']);
    expect(groups[0]!.agents[0]!.sub).toBe('Claude Code · слушает');
    expect(groups[0]!.channels.map((c) => c.title)).toEqual(['Руководители ПВЗ', 'Личка']);
    expect(groups[1]!.channels.map((c) => c.title)).toEqual(['Банк']);
  });

  it('скрытые проекты не входят', () => {
    expect(build({ sessions: [session()], hiddenProjects: [3] })).toEqual([]);
  });

  it('workSummary: X в работе из Y (незакрытые хаба + живые внешние)', () => {
    const lines = buildAgentLines({}, [sub(), sub({ id: 41, role: 'другой', live: false })]);
    expect(workSummary([session({ state: 'running' }), session({ id: 6 }), session({ id: 7, closed_ts: NOW })], lines)).toEqual({
      running: 1,
      total: 3,
    });
  });
});

describe('Ф41-6a: BoardList', () => {
  function mountList() {
    return mount(BoardList, { props: { nowMs: NOW } });
  }

  it('строки сессий: клик открывает столбец, aria-pressed следует за доской; оркестратор — столбец owner_channel', async () => {
    store.state.sessions.items = { 5: session({ pending_requests: 1 }) };
    const w = mountList();
    const row = w.get('[data-session-id="5"]');
    expect(row.attributes('aria-pressed')).toBe('false');
    expect(row.text()).toContain('программист');
    expect(row.text()).toContain('ждёт тебя');
    await row.trigger('click');
    expect(boardState.layout.open).toEqual([{ kind: 'session', id: 5 }]);
    expect(w.get('[data-session-id="5"]').attributes('aria-pressed')).toBe('true');
    await w.get('.board-row--owner').trigger('click');
    expect(boardState.layout.open).toEqual([{ kind: 'session', id: 5 }, { kind: 'owner_channel' }]);
    // повторный клик по открытому не дублирует
    await w.get('[data-session-id="5"]').trigger('click');
    expect(boardState.layout.open).toHaveLength(2);
  });

  it('Ф98-1: у строки сессии вторая строка — движок и модель, полный текст в title', () => {
    store.state.sessions.items = {
      5: session({ engine: 'opencode', model: 'deepseek/deepseek-flash', effort: 'max' }),
    };
    const w = mountList();
    const row = w.get('[data-session-id="5"]');
    const line = row.get('.board-row-engine');
    expect(line.text()).toBe('OpenCode · deepseek/deepseek-flash · max');
    expect(line.attributes('title')).toBe('OpenCode · deepseek/deepseek-flash · max');
    expect(row.get('.board-row-name').text()).toBe('программист');
    expect(row.text()).toContain('ждёт входа');
  });

  it('внешняя сессия — строка с движком и «слушает»; клик — столбец agent', async () => {
    store.state.peerMsgs.agentSubs = [sub()];
    const w = mountList();
    const row = w.get('.board-row--agent');
    expect(row.text()).toContain('Claude Code · слушает');
    await row.trigger('click');
    expect(boardState.layout.open[0]).toMatchObject({ kind: 'agent', key: 'p3:маркетолог:1' });
  });

  it('«беседы ждут · K»: клик по беседе — прежний переход на вкладку проекта (столбец беседы будет в 6б)', async () => {
    store.state.home = { ...store.state.home, react: [reactItem(22, 3, 'Личка с тимлидом')] };
    const open = vi.spyOn(store, 'openChannelAt').mockResolvedValue(undefined);
    const w = mountList();
    expect(w.get('.board-group-chats').text()).toBe('▸ беседы ждут · 1');
    expect(w.find('[data-channel-id="22"]').exists()).toBe(false); // по умолчанию свёрнуты
    await w.get('.board-group-chats').trigger('click');
    expect(w.get('.board-group-chats').attributes('aria-expanded')).toBe('true');
    await w.get('[data-channel-id="22"]').trigger('click');
    expect(open).toHaveBeenCalledWith(3, 22, {}, { column: true });
    expect(boardState.layout.open).toEqual([]);
  });

  it('день проекта закрыт — его ждущих бесед в списке нет', () => {
    store.state.projects = store.state.projects.map((p) => (p.id === 3 ? { ...p, workday: true, day_open: false } : p));
    store.state.home = { ...store.state.home, react: [reactItem(22, 3, 'Личка с тимлидом')] };
    const w = mountList();
    expect(w.find('.board-group-chats').exists()).toBe(false);
  });

  it('«Скрыть проект» убирает группу, «показать» возвращает; запись в «Скрыты: …»', async () => {
    store.state.sessions.items = { 5: session() };
    const w = mountList();
    await w.get('.board-group-menu').trigger('click');
    await w.get('.board-group-hide').trigger('click');
    expect(boardState.layout.hidden_projects).toEqual([3]);
    expect(w.find('.board-group').exists()).toBe(false);
    expect(w.get('.board-hidden').text()).toContain('Скрыты: Umbrella');
    await w.get('.board-hidden-show').trigger('click');
    expect(boardState.layout.hidden_projects).toEqual([]);
    expect(w.find('.board-group').exists()).toBe(true);
  });

  it('свёрнутый список — узкая полоса с кружками проектов', () => {
    store.state.sessions.items = { 5: session({ pending_requests: 1 }) };
    boardState.layout.list_collapsed = true;
    const w = mountList();
    expect(w.classes()).toContain('board-list--collapsed');
    expect(w.findAll('.board-list-dot')).toHaveLength(1);
    expect(w.get('.board-list-dot .badge').text()).toBe('1');
    expect(w.find('.board-row').exists()).toBe(false);
  });

  it('«Новая сессия» открывает форму с выбором проекта', async () => {
    const w = mountList();
    await w.get('.board-list-new').trigger('click');
    expect(w.find('form.session-new').exists()).toBe(true);
    expect(w.find('.session-new-project').exists()).toBe(true);
  });
});

describe('Ф41-6a: «Ждёт тебя · N»', () => {
  it('панель закрыта по умолчанию; кнопка проекта раскрывает её на нём; «Закрыть» и Esc закрывают', async () => {
    store.state.sessions.items = { 5: session({ pending_requests: 2 }) };
    const w = mount(WaitBar);
    await flushPromises();
    expect(w.find('.wait-bar-pop').exists()).toBe(false); // Ф90-2: умолчание — закрыта, в поток не входит
    toggleWait('all');
    await flushPromises();
    expect(w.get('.wait-bar-title').text()).toBe('Ждёт тебя · 2');
    expect(w.get('.wait-bar-project').text()).toBe('Umbrella 2 ▾');
    expect(w.find('.wait-bar-panel').exists()).toBe(true);
    await w.get('.wait-bar-toggle').trigger('click');
    expect(boardState.waitShown).toBe(false);
    expect(w.find('.wait-bar-panel').exists()).toBe(false);
    toggleWait('all');
    await flushPromises();
    await w.get('.wait-bar-project').trigger('click');
    expect(boardState.layout.wait_open).toBe(true);
    expect(boardState.waitProject).toBe(3);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(boardState.waitShown).toBe(false);
    expect(w.find('.wait-bar-pop').exists()).toBe(false);
    w.unmount();
  });

  it('клик мимо (по подложке) закрывает панель', async () => {
    const w = mount(WaitBar);
    toggleWait('all');
    await flushPromises();
    await w.get('[data-test="wait-backdrop"]').trigger('click');
    expect(boardState.waitShown).toBe(false);
    w.unmount();
  });

  it('кнопка-счётчик в верхней полосе: «Ждёт тебя · N» открывает и закрывает панель', async () => {
    store.state.sessions.items = { 5: session({ pending_requests: 2 }) };
    store.state.homeOpen = true;
    const w = mount(TopBar);
    await flushPromises();
    const btn = w.get('[data-test="top-wait"]');
    expect(btn.text()).toContain('Ждёт тебя · 2');
    await btn.trigger('click');
    expect(boardState.waitShown).toBe(true);
    await btn.trigger('click');
    expect(boardState.waitShown).toBe(false);
    store.state.homeOpen = false;
    await flushPromises();
    expect(w.find('[data-test="top-wait"]').exists()).toBe(false);
    w.unmount();
  });

  it('ничего не ждёт — серая строка', async () => {
    toggleWait('all');
    const w = mount(WaitBar);
    expect(w.get('.wait-bar-title').text()).toBe('Ничего не ждёт');
    w.unmount();
  });

  it('число совпадает с бейджем вкладки «Ждёт» телефона на одном наборе данных', async () => {
    store.state.sessions.items = { 5: session({ pending_requests: 2 }), 6: session({ id: 6, project_id: 4, wait_reason: 'heavy', sub: { id: 1, mode: 'messages', msg_filter: 'all', keywords: [], exclude: [], batch_min: null, accounts: null, pending: 3 } }) };
    store.state.home = { ...store.state.home, drafts: [{ id: 9, channel_id: 21, project_id: 3, channel_title: 'Т', status: 'draft' } as unknown as OutboxListItem] };
    toggleWait('all');
    const bar = mount(WaitBar);
    const phone = mount(PhoneShell);
    await flushPromises();
    const n = buildWaiting({
      projects: store.state.projects,
      home: store.state.home,
      
      sessions: store.state.sessions,
      joins: Object.values(store.state.joins.items),
      leaves: Object.values(store.state.leaves.items),
      reminders: Object.values(store.state.reminders.items),
    }).length;
    expect(n).toBeGreaterThan(0);
    expect(bar.get('.wait-bar-title').text()).toBe(`Ждёт тебя · ${n}`);
    expect(phone.get('.phone-tab--wait .badge').text()).toBe(String(n));
    expect(waitSummary(buildWaiting({ projects: store.state.projects, home: store.state.home, sessions: store.state.sessions }), []).total).toBe(n);
    bar.unmount();
    phone.unmount();
  });

  it('скрытый проект не получает кнопку, но входит в общее число', async () => {
    store.state.sessions.items = { 5: session({ pending_requests: 1 }) };
    boardState.layout.hidden_projects = [3];
    toggleWait('all');
    const w = mount(WaitBar);
    expect(w.get('.wait-bar-title').text()).toBe('Ждёт тебя · 1');
    expect(w.find('.wait-bar-project').exists()).toBe(false);
    w.unmount();
  });
});

describe('Ф41-6a: BoardToolbar', () => {
  it('столбцы − N +, свернуть список, «сессий в работе X из Y», суммы $ нет', async () => {
    store.state.sessions.items = { 5: session({ state: 'running' }), 6: session({ id: 6 }) };
    const w = mount(BoardToolbar);
    expect(w.find('.board-cols-count').exists()).toBe(false); // Ф90-2: вид и столбцы — в выпадашке «Вид ▾»
    await w.get('.board-toolbar-viewbtn').trigger('click');
    expect(w.get('.board-cols-count').text()).toBe('3');
    await w.get('.board-cols-plus').trigger('click');
    expect(w.get('.board-cols-count').text()).toBe('4');
    await w.get('.board-cols-minus').trigger('click');
    await w.get('.board-cols-minus').trigger('click');
    await w.get('.board-cols-minus').trigger('click');
    expect(w.get('.board-cols-count').text()).toBe('1');
    expect(w.get('.board-cols-minus').attributes('disabled')).toBeDefined();
    expect(w.get('.board-toolbar-summary').text()).toBe('сессий в работе 1 из 2');
    expect(w.get('.board-toolbar-summary').text()).not.toContain('$');
    expect(w.get('.board-toolbar-list').attributes('title')).toBe('Свернуть список');
    await w.get('.board-toolbar-list').trigger('click');
    expect(w.get('.board-toolbar-list').attributes('title')).toBe('Показать список');
  });
});
