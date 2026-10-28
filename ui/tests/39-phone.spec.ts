// Ф10-6 B4/B5 (`docs/architecture.md`): телефон — нижние вкладки и стартовая «Ждёт»; `util/waiting` —
// состав и порядок групп, счётчик; тап по реакции открывает беседу и вкладку «Беседы»; «Беседы»: список -> лента
// («‹») -> тред; «Назад»: `pushState` ровно при росте уровня, `popstate` закрывает тред, затем беседу; смена
// беседы историю не растит; в `full` история не трогается; «Ждёт» -> «Беседы» сохраняет открытую беседу.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, HomeReactItem, OutboxListItem, Project } from '../src/api/types';
import MainApp from '../src/components/MainApp.vue';
import { store } from '../src/store/instance';
import { waitSummary } from '../src/util/boardList';
import { initLayout } from '../src/util/layout';
import { buildWaiting, homeDecisionCount, type WaitingInput } from '../src/util/waiting';
import { fakeMedia } from './media';

function project(overrides: Partial<Project>): Project {
  return { id: 1, name: 'Initech', color: '#00aa00', sort: 0, muted: false, workdir: null, unread: 0, ...overrides };
}

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 7,
    ext_id: 'c7',
    kind: 'group',
    title: 'фронт',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 1,
    ...overrides,
  };
}

function hubSession(id: number, projectId: number, overrides: Record<string, unknown> = {}): never {
  return { id, project_id: projectId, closed_ts: null, pending_requests: 0, ...overrides } as never;
}

function sessionRequest(id: number, sessionId: number, projectId: number, overrides: Record<string, unknown> = {}): never {
  return { id, session_id: sessionId, project_id: projectId, kind: 'permission', tool: 'Bash', input: { command: 'ls' }, status: 'pending', ...overrides } as never;
}

function draft(overrides: Partial<OutboxListItem>): OutboxListItem {
  return {
    id: 50,
    channel_id: 7,
    project_id: 1,
    channel_title: 'фронт',
    text: 'ответ',
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: 1000,
    sent_ts: null,
    ext_id: null,
    ...overrides,
  };
}

function reactItem(overrides: Partial<HomeReactItem> = {}): HomeReactItem {
  return {
    channel: channel({ id: 7, project_id: 1, unread_react: 2 }),
    project: { id: 1, name: 'Initech', color: '#00aa00' },
    last: { sender: 'Пётр', excerpt: 'Когда?', ts: 1000, message_id: 70 },
    card: null,
    draft_count: 0,
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

/** Ядро: беседы проектов из `channels`, «Нужна реакция» из `react`, остальное — пусто. */
function stubFetch(
  opts: { channels?: Record<number, Channel[]>; react?: HomeReactItem[] } = {},
): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      const m = /^\/api\/v1\/projects\/(-?\d+)\/channels$/.exec(url.pathname);
      if (m) return jsonResponse(opts.channels?.[Number(m[1])] ?? []);
      if (url.pathname === '/api/v1/home/react') return jsonResponse(opts.react ?? []);
      if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
      if (url.pathname.endsWith('/events')) return jsonResponse([]);
      if (url.pathname === '/api/v1/status') {
        return jsonResponse({ accounts: [], dead_letter: {}, outbox_pending: 0, outbox_approval: 0, outbox_unknown: 0, max_seq: 1, now: 0 });
      }
      if (['/api/v1/outbox', '/api/v1/agent/runs', '/api/v1/inbox'].includes(url.pathname)) {
        return jsonResponse([]);
      }
      return jsonResponse({ ok: true });
    }),
  );
}

function resetStore(): void {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  store.state.sync.threads = {};
  store.state.projects = [];
  store.state.currentProjectId = null;
  store.state.currentChannelId = null;
  store.state.openThread = null;
  store.state.homeOpen = true;
  store.state.homeAnchor = null;
  store.state.focus = null;
  store.state.status = null;
  store.state.home = { react: [], drafts: [], runs: [], sent: [], loadedAt: null, error: null };
  store.state.lastChannelByProject = {};
  store.state.phoneTab = 'wait';
  store.state.boardFocus = null;
}

let pushState: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetStore();
  window.history.replaceState(null, '');
  pushState = vi.spyOn(window.history, 'pushState');
  initLayout(fakeMedia(390).matchMedia);
});

afterEach(() => {
  initLayout(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** `popstate`, как после «Назад» браузера. */
async function popState(): Promise<void> {
  window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }));
  await flushPromises();
}

// --- util/waiting ---------------------------------------------------------------------------------------------

describe('Ф10-6 B4: util/waiting — состав и порядок групп', () => {
  function input(): WaitingInput {
    const sessions = {
      items: { 1: hubSession(1, 3, { pending_requests: 1 }), 4: hubSession(4, 1, { pending_requests: 1 }), 5: hubSession(5, 1) },
      pending: { 1: [sessionRequest(11, 1, 3)], 4: [sessionRequest(12, 4, 1)] },
      stale: { 9: sessionRequest(9, 5, 1, { status: 'expired' }) },
    };
    return {
      projects: [project({ id: 1, name: 'Initech' }), project({ id: 3, name: 'Umbrella', color: '#ff0000' })],
      home: {
        drafts: [draft({ id: 51, created_ts: 3000 }), draft({ id: 50, created_ts: 2000, project_id: null })],
        react: [reactItem()],
      },
      sessions: sessions as never,
    };
  }

  it('один вход: счёт полосы по проекту = счёту столбца (homeDecisionCount)', () => {
    const inp: WaitingInput = {
      projects: [project({ id: 1, name: 'Initech' })],
      home: { drafts: [draft({ id: 51, project_id: 1 })], react: [] },
    };
    const sum = waitSummary(buildWaiting(inp), []);
    expect(sum.total).toBe(homeDecisionCount(inp));
    expect(sum.byProject.map((e) => e.count)).toEqual([1]);
  });

  it('карточки сессий (и протухшие) -> черновики (старые сверху) -> реакция', () => {
    const items = buildWaiting(input());
    expect(items.map((i) => i.key)).toEqual([
      'session-stale-9', // внутри проекта — по id запроса; Initech раньше Umbrella (порядок проектов)
      'session-request-12',
      'session-request-11',
      'draft-50',
      'draft-51',
      'react-7',
    ]);
    const stale = items[0]!;
    expect(stale.kind === 'session_request' && stale.stale).toBe(true);
    // метка «цвет · проект»: из проектов; без проекта — «Неразобранное»
    expect(items[2]!.project).toEqual({ id: 3, name: 'Umbrella', color: '#ff0000' });
    expect(items[3]!.project).toEqual({ id: null, name: 'Неразобранное', color: null });
  });

  it('счётчики: «Ждёт» — все строки; «Главная» — карточки сессий + черновики', () => {
    expect(buildWaiting(input())).toHaveLength(6);
    expect(homeDecisionCount(input())).toBe(5);
    const empty: WaitingInput = { projects: [], home: { drafts: [], react: [] } };
    expect(buildWaiting(empty)).toEqual([]);
    expect(homeDecisionCount(empty)).toBe(0);
  });
});

// --- оболочка телефона --------------------------------------------------------------------------------------------

describe('Ф10-6 B4: нижние вкладки', () => {
  it('после входа — «Ждёт»; пять вкладок; нет верхних вкладок проектов; «Ждёт · N»', async () => {
    stubFetch({ react: [reactItem()] });
    store.state.projects = [project({ id: 1 })];
    const wrapper = mount(MainApp);
    await flushPromises();
    expect(wrapper.find('.phone-shell').exists()).toBe(true);
    expect(wrapper.find('nav.project-tabs').exists()).toBe(false);
    const tabs = wrapper.findAll('nav.phone-tabs button');
    expect(tabs.map((t) => t.text().replace(/\s+/g, ' '))).toEqual(['Ждёт · 1', 'Проекты', 'Беседы', 'Сессии', 'Агент']);
    expect(tabs[0]!.attributes('aria-current')).toBe('page');
    expect(wrapper.get('.phone-title').text()).toBe('Ждёт');
    expect(wrapper.find('.wait-list').exists()).toBe(true);
    expect(wrapper.get('.wait-row--react').text()).toContain('Initech · фронт');
    expect(wrapper.get('.wait-row--react').text()).toContain('Пётр: Когда?');
    wrapper.unmount();
  });

  it('пусто — «Ничего не ждёт.»; вкладки переключают разделы', async () => {
    stubFetch();
    const wrapper = mount(MainApp);
    await flushPromises();
    expect(wrapper.get('.wait-empty').text()).toBe('Ничего не ждёт.');
    await wrapper.get('.phone-tab--projects').trigger('click');
    await flushPromises();
    expect(wrapper.find('.home-view').exists()).toBe(true);
    expect(store.state.homeOpen).toBe(true);
    await wrapper.get('.phone-tab--orch').trigger('click');
    await flushPromises();
    expect(wrapper.find('.phone-sessions').exists()).toBe(true); // Ф41-6б: вкладка «Сессии»
    expect(wrapper.get('.phone-title').text()).toBe('Сессии');
    expect(wrapper.get('.phone-tab--orch').attributes('aria-current')).toBe('page');
    await wrapper.get('.phone-tab--chats').trigger('click');
    await flushPromises();
    // проекта нет — открыты «Все»
    expect(store.state.currentProjectId).toBe(-2);
    expect(store.state.homeOpen).toBe(false);
    expect(wrapper.find('nav.column-channels').exists()).toBe(true);
    wrapper.unmount();
  });

  it('тап по реакции в «Ждёт» открывает беседу и вкладку «Беседы»', async () => {
    stubFetch({ react: [reactItem()], channels: { 1: [channel({ id: 7 })] } });
    store.state.projects = [project({ id: 1 })];
    const wrapper = mount(MainApp);
    await flushPromises();
    await wrapper.get('.wait-row--react').trigger('click');
    await flushPromises();
    expect(store.state.phoneTab).toBe('chats');
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.currentProjectId).toBe(1);
    expect(store.state.currentChannelId).toBe(7);
    expect(store.state.focus).toMatchObject({ channelId: 7, messageId: 70 });
    expect(wrapper.get('.phone-tab--chats').attributes('aria-current')).toBe('page');
    expect(wrapper.find('.column-feed').exists()).toBe(true);
    expect(pushState).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });
});

describe('Ф10-6 B4: «Беседы» и «Назад» (решение 8)', () => {
  async function mountChats() {
    stubFetch({ channels: { 1: [channel({ id: 7 }), channel({ id: 8, title: 'бэк' })] } });
    store.state.projects = [project({ id: 1 })];
    store.state.sync.channelsById = { 7: channel({ id: 7 }), 8: channel({ id: 8, title: 'бэк' }) };
    const wrapper = mount(MainApp, { attachTo: document.body });
    await flushPromises();
    await wrapper.get('.phone-tab--chats').trigger('click');
    await flushPromises();
    await wrapper.get('.project-chip[data-project-id="1"]').trigger('click');
    await flushPromises();
    return wrapper;
  }

  it('список -> лента («‹» назад к списку) -> тред; pushState ровно при росте уровня', async () => {
    const wrapper = await mountChats();
    expect(store.state.currentProjectId).toBe(1);
    expect(wrapper.find('nav.column-channels').exists()).toBe(true);
    expect(wrapper.find('.column-feed').exists()).toBe(false);
    expect(pushState).not.toHaveBeenCalled();

    const channelButton = wrapper.findAll('nav.column-channels button').find((b) => b.text().includes('фронт'));
    await channelButton!.trigger('click');
    await flushPromises();
    expect(wrapper.find('nav.column-channels').exists()).toBe(false);
    expect(wrapper.find('.project-chips').exists()).toBe(false);
    expect(wrapper.get('.column-feed h2').text()).toBe('фронт');
    expect(pushState).toHaveBeenCalledTimes(1);

    // смена беседы при открытой — историю не растит
    await store.openChannel(8);
    await flushPromises();
    expect(pushState).toHaveBeenCalledTimes(1);

    await store.openThread(8, 'P1');
    await flushPromises();
    expect(pushState).toHaveBeenCalledTimes(2);
    expect(wrapper.get('.chats-view').classes()).toContain('chats-view--thread');
    expect(wrapper.get('.column-thread').classes()).toContain('column-thread--phone');
    expect(wrapper.find('.column-thread .back-button').exists()).toBe(true);
    expect(wrapper.find('.column-thread .thread-close').exists()).toBe(false);

    // «Назад» браузера: тред, затем беседа
    await popState();
    expect(store.state.openThread).toBeNull();
    expect(store.state.currentChannelId).toBe(8);
    await popState();
    expect(store.state.currentChannelId).toBeNull();
    expect(wrapper.find('nav.column-channels').exists()).toBe(true);
    await popState(); // дальше закрывать нечего
    expect(store.state.currentProjectId).toBe(1);
    wrapper.unmount();
  });

  it('«‹» ленты: верхняя запись истории наша -> history.back(); иначе закрывает сам', async () => {
    const wrapper = await mountChats();
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => {
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    await store.openChannel(7);
    await flushPromises();
    expect(pushState).toHaveBeenCalledTimes(1);
    await wrapper.get('.column-feed .back-button').trigger('click');
    await flushPromises();
    expect(back).toHaveBeenCalledTimes(1);
    expect(store.state.currentChannelId).toBeNull();

    // запись истории не наша (например, после перезагрузки страницы) — закрывает сам, history.back() не зовётся
    await store.openChannel(7);
    await flushPromises();
    window.history.replaceState(null, '');
    await wrapper.get('.column-feed .back-button').trigger('click');
    await flushPromises();
    expect(back).toHaveBeenCalledTimes(1);
    expect(store.state.currentChannelId).toBeNull();
    wrapper.unmount();
  });

  it('«Ждёт» -> «Беседы» сохраняет открытую беседу; на «Ждёт» присутствия нет', async () => {
    const wrapper = await mountChats();
    await store.openChannel(7);
    await flushPromises();
    const target = () => (store as unknown as { presenceTarget(): unknown }).presenceTarget();
    expect(target()).toEqual({ channelId: 7, threadExtId: null });
    await wrapper.get('.phone-tab--wait').trigger('click');
    await flushPromises();
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.currentChannelId).toBe(7);
    expect(target()).toBeNull();
    await wrapper.get('.phone-tab--chats').trigger('click');
    await flushPromises();
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.currentChannelId).toBe(7);
    expect(wrapper.get('.column-feed h2').text()).toBe('фронт');
    wrapper.unmount();
  });
});

describe('Ф10-6 B4: вне телефона история не трогается', () => {
  it('full: открытие беседы и треда — без pushState; popstate ничего не закрывает', async () => {
    initLayout(null);
    stubFetch();
    store.state.sync.channelsById = { 7: channel({ id: 7 }) };
    store.state.currentProjectId = 1;
    await store.openChannel(7);
    await store.openThread(7, 'P1');
    expect(pushState).not.toHaveBeenCalled();
    store.onPhonePopState();
    expect(store.state.openThread).not.toBeNull();
    expect(store.state.currentChannelId).toBe(7);
    store.goBack('thread');
    expect(store.state.openThread).toBeNull();
  });
});
