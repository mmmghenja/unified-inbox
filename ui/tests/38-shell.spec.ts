// Ф10-6 B5 (`docs/architecture.md`): оболочка на `full`/`half` — режим из `matchMedia` и
// его смена на лету; вкладки проектов (состав, порядок, счётчики, клик, `aria-current`); компактный блок полосы
// (аккаунты, нулевые счётчики очередей); вкладка помнит беседу; присутствие на главной; `half` — тред поверх
// ленты и Esc; меню «⋯» шапки беседы.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Account, Channel, OutboxListItem, Project, StatusResponse } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import ChatsView from '../src/components/ChatsView.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';
import { createStore } from '../src/store/store';
import { initLayout, layout } from '../src/util/layout';
import { fakeMedia } from './media';

// --- данные ---------------------------------------------------------------------------------------------------

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
    project_id: 3,
    ...overrides,
  };
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

function account(overrides: Partial<Account>): Account {
  return { id: 1, source: 'telegram', title: 'Телеграм', status: 'ok', last_ok_ts: null, last_error: null, flood_until: null, ...overrides };
}

function status(overrides: Partial<StatusResponse>): StatusResponse {
  return {
    accounts: [],
    dead_letter: {},
    outbox_pending: 0,
    outbox_approval: 0,
    outbox_unknown: 0,
    max_seq: 1,
    now: 0,
    ...overrides,
  };
}

function jsonResponse(body: unknown, statusCode = 200): Response {
  return new Response(JSON.stringify(body), { status: statusCode, headers: { 'content-type': 'application/json' } });
}

/** Ответы ядра по пути; беседы проекта — из `channels`. */
function stubFetch(channels: Record<number, Channel[]> = {}): { paths: string[] } {
  const paths: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      paths.push(url.pathname);
      const m = /^\/api\/v1\/projects\/(-?\d+)\/channels$/.exec(url.pathname);
      if (m) return jsonResponse(channels[Number(m[1])] ?? []);
      if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
      if (url.pathname === '/api/v1/outbox') return jsonResponse([]);
      return jsonResponse({ ok: true });
    }),
  );
  return { paths };
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
  store.state.status = null;
  store.state.home = { react: [], drafts: [], runs: [], sent: [], loadedAt: null, error: null };
  store.state.lastChannelByProject = {};
  store.state.phoneTab = 'wait';
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetStore();
  initLayout(null);
});

afterEach(() => {
  initLayout(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --- B1: режим раскладки ------------------------------------------------------------------------------------------

describe('Ф10-6 B1: режим раскладки из matchMedia', () => {
  it('matchMedia нет — full; 1440 — full, 960 — half, 390 — phone; смена ширины на лету', () => {
    initLayout(null);
    expect(layout.mode).toBe('full');
    const media = fakeMedia(1440);
    initLayout(media.matchMedia);
    expect(layout.mode).toBe('full');
    media.setWidth(960);
    expect(layout.mode).toBe('half');
    media.setWidth(390);
    expect(layout.mode).toBe('phone');
    media.setWidth(599);
    expect(layout.mode).toBe('phone');
    media.setWidth(600);
    expect(layout.mode).toBe('half');
    media.setWidth(1280);
    expect(layout.mode).toBe('full');
  });

  it('повторный initLayout снимает прежнюю подписку', () => {
    const first = fakeMedia(1440);
    initLayout(first.matchMedia);
    initLayout(fakeMedia(960).matchMedia);
    expect(layout.mode).toBe('half');
    first.setWidth(390); // старая подписка молчит
    expect(layout.mode).toBe('half');
  });
});

// --- B2: вкладки ---------------------------------------------------------------------------------------------------

describe('Ф10-6 B2: вкладки проектов', () => {
  const projects = (): Project[] => [
    project({ id: 1, name: 'Initech', unread: 9, unread_react: 2 }),
    project({ id: 3, name: 'Umbrella', unread: 4, unread_react: 0 }),
    project({ id: -1, name: 'Неразобранное', color: null, unread: 5, unread_react: 1 }),
    project({ id: -2, name: 'Все', color: null, unread: 18, unread_react: 3 }),
  ];

  it('состав и порядок: Главная · проекты (порядок API) · Неразобранное · Все (без счётчика) · Claude; серых счётчиков нет', () => {
    store.state.projects = projects();
    const wrapper = mount(TopBar);
    const tabs = wrapper.findAll('nav.project-tabs > button');
    expect(tabs.map((t) => t.find('.project-tab-name').text())).toEqual([
      'Главная',
      'Initech',
      'Umbrella',
      'Неразобранное',
      'Все',
    ]);
    expect(tabs[1]!.find('.badge-react').text()).toBe('2');
    // Ф20-4 A1: серого счётчика «к сведению» у вкладки больше нет (у проекта 3 — только серые 4)
    expect(tabs[2]!.find('.badge').exists()).toBe(false);
    expect(wrapper.find('nav.project-tabs .badge-quiet').exists()).toBe(false);
    expect(tabs[3]!.find('.badge-react').text()).toBe('1');
    expect(tabs[4]!.find('.badge').exists()).toBe(false);
    expect(tabs[4]!.classes()).toContain('project-tab--all');
    // без решений — у «Главной» счётчика нет
    expect(tabs[0]!.find('.badge').exists()).toBe(false);
    wrapper.unmount();
  });

  it('счётчик «Главной» — карточки сессий + черновики (не беседы)', async () => {
    store.state.projects = projects();
    const wrapper = mount(TopBar);
    store.state.sessions.items = { 5: { id: 5, project_id: 1, closed_ts: null, pending_requests: 1 } as never };
    store.state.sessions.pending = {
      5: [{ id: 11, session_id: 5, project_id: 1, kind: 'permission', tool: 'Bash', input: { command: 'ls' }, status: 'pending' } as never],
    };
    store.state.home.drafts = [draft({ id: 50 })];
    store.state.home.react = [
      {
        channel: channel({ id: 7 }),
        project: { id: 3, name: 'Umbrella', color: null },
        last: { sender: 'Пётр', excerpt: 'привет', ts: 1000, message_id: 1 },
        card: null,
        draft_count: 0,
      },
    ];
    await flushPromises();
    const home = wrapper.get('.project-tab--home .badge');
    expect(home.text()).toBe('2');
    expect(home.classes()).toContain('badge-react');
    wrapper.unmount();
  });

  it('клик: «Главная» -> openHome, проект -> openProject; aria-current у активной', async () => {
    stubFetch();
    store.state.projects = projects();
    const openHome = vi.spyOn(store, 'openHome');
    const openProject = vi.spyOn(store, 'openProject');
    const wrapper = mount(TopBar);
    expect(wrapper.get('.project-tab--home').attributes('aria-current')).toBe('page');

    await wrapper.get('.project-tab[data-project-id="3"]').trigger('click');
    await flushPromises();
    expect(openProject).toHaveBeenCalledWith(3);
    expect(store.state.homeOpen).toBe(false);
    expect(wrapper.get('.project-tab[data-project-id="3"]').attributes('aria-current')).toBe('page');
    expect(wrapper.get('.project-tab--home').attributes('aria-current')).toBeUndefined();
    expect(wrapper.findAll('[aria-current="page"]')).toHaveLength(1);

    await wrapper.get('.project-tab[data-project-id="-2"]').trigger('click');
    await flushPromises();
    expect(openProject).toHaveBeenLastCalledWith(-2);
    expect(wrapper.get('.project-tab[data-project-id="-2"]').attributes('aria-current')).toBe('page');

    await wrapper.get('.project-tab--home').trigger('click');
    expect(openHome).toHaveBeenCalled();
    expect(store.state.homeOpen).toBe(true);
    expect(wrapper.get('.project-tab--home').attributes('aria-current')).toBe('page');
    wrapper.unmount();
  });
});

// --- B2: компактный блок ---------------------------------------------------------------------------------------------

describe('Ф10-6 B2 / Ф20-4 A: правая часть полосы — тревоги сами, остальное в меню «⋯»', () => {
  it('«аккаунты ✓» — только в меню; неисправный аккаунт виден в полосе без меню', async () => {
    store.state.status = status({
      accounts: [account({ id: 1, title: 'Телеграм' }), account({ id: 2, source: 'slack', title: 'Globex' })],
    });
    const wrapper = mount(TopBar);
    const items = () => wrapper.findAll('ul.account-statuses li');
    expect(items()).toEqual([]);
    expect(wrapper.find('.accounts-ok').exists()).toBe(false);
    await wrapper.get('.top-menu-button').trigger('click');
    expect(wrapper.get('.top-menu-popup .accounts-ok').text()).toBe('аккаунты ✓');
    expect(wrapper.get('.accounts-ok').attributes('title')).toBe('Телеграм: ok\nGlobex: ok');
    await wrapper.get('.top-menu-button').trigger('click');

    store.state.status = status({
      accounts: [account({ id: 1, title: 'Телеграм' }), account({ id: 2, title: 'Globex', status: 'auth_required' })],
    });
    await flushPromises();
    // меню закрыто — тревога всё равно в полосе
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);
    expect(items().map((li) => li.text())).toEqual(['Globex: auth_required']);
    expect(items()[0]!.classes()).toContain('status-auth_required');
    await wrapper.get('.top-menu-button').trigger('click');
    expect(wrapper.find('.accounts-ok').exists()).toBe(false);
    wrapper.unmount();
  });

  it('счётчики очередей: «неизвестно» и dead_letter — в полосе, «ждёт» — в меню и ведёт на черновики главной', async () => {
    stubFetch();
    store.state.status = status({});
    const wrapper = mount(TopBar);
    expect(wrapper.text()).not.toContain('ждёт:');
    expect(wrapper.text()).not.toContain('неизвестно:');
    expect(wrapper.text()).not.toContain('dead_letter');

    store.state.status = status({ outbox_approval: 2, outbox_unknown: 1, dead_letter: { telegram: 3 } });
    store.state.homeOpen = false;
    store.state.currentProjectId = 3;
    await flushPromises();
    expect(wrapper.find('.queue-counter--drafts').exists()).toBe(false);
    expect(wrapper.get('.queue-counter--unknown').text()).toBe('неизвестно: 1');
    expect(wrapper.get('.queue-counter--dead').text()).toBe('dead_letter: 3');

    await wrapper.get('.top-menu-button').trigger('click');
    expect(wrapper.get('.top-menu-popup .queue-counter--drafts').text()).toBe('ждёт: 2');
    await wrapper.get('.queue-counter--drafts').trigger('click');
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.homeAnchor).toBe('drafts');

    await wrapper.get('.queue-counter--unknown').trigger('click');
    await flushPromises();
    expect(wrapper.find('.queue-panel').exists()).toBe(true);
    wrapper.unmount();
  });

  it('связь в порядке — «● N с» только в меню; поток молчит или /sync упал — красным в полосе', async () => {
    store.state.staleness.lastFrameAt = Date.now() - 5000;
    store.state.staleness.stale = false;
    const wrapper = mount(TopBar);
    expect(wrapper.find('.staleness').exists()).toBe(false);
    await wrapper.get('.top-menu-button').trigger('click');
    const ok = wrapper.get('.top-menu-popup .staleness');
    expect(ok.text()).toMatch(/^\d+ с$/);
    expect(ok.find('.status-dot').exists()).toBe(true);
    expect(ok.classes()).toContain('staleness--ok');
    await wrapper.get('.top-menu-button').trigger('click');

    store.state.staleness.stale = true;
    await flushPromises();
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);
    expect(wrapper.get('.staleness').text()).toMatch(/^лента не обновляется \(\d+ с\)$/);
    expect(wrapper.get('.staleness').classes()).toContain('stale');
    store.state.syncError = { at: Date.now(), message: 'x' };
    await flushPromises();
    expect(wrapper.get('.sync-error').text()).toContain('синхронизация не удалась');
    store.state.syncError = null;
    store.state.staleness.stale = false;
    store.state.staleness.lastFrameAt = null;
    wrapper.unmount();
  });
});

describe('Ф20-4 A2: меню «⋯»', () => {
  it('кнопка с aria; открывается и закрывается по повторному клику, Esc и клику мимо; пункты на месте', async () => {
    const wrapper = mount(TopBar, { attachTo: document.body });
    const button = wrapper.get('.top-menu-button');
    expect(button.attributes('aria-label')).toBe('Меню');
    expect(button.attributes('aria-haspopup')).toBe('menu');
    expect(button.attributes('aria-expanded')).toBe('false');
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);

    await button.trigger('click');
    expect(button.attributes('aria-expanded')).toBe('true');
    const items = wrapper.findAll('.top-menu-popup [role="menuitem"]').map((i) => i.text());
    expect(items).toEqual(['Новое сообщение', 'Поиск', 'Агент', 'Написать…', 'Терминал', 'Агенты', 'Раскладка', 'Люди', 'Машины', 'Мои данные', 'Напоминания', 'Автопилот', 'Доступ', 'Движок: Claude Code']);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);
    expect(button.attributes('aria-expanded')).toBe('false');

    await button.trigger('click');
    expect(wrapper.find('.top-menu-popup').exists()).toBe(true);
    document.body.click();
    await flushPromises();
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);

    await button.trigger('click');
    await wrapper.get('.top-menu-popup').trigger('click'); // клик внутри меню не закрывает
    expect(wrapper.find('.top-menu-popup').exists()).toBe(true);
    await wrapper.get('.access-open').trigger('click'); // выбор пункта закрывает
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);
    wrapper.unmount();
  });

  it('«Поиск» — два нажатия до поля: «⋯», «Поиск»; фокус в поле; Esc закрывает', async () => {
    const wrapper = mount(TopBar, { attachTo: document.body });
    await wrapper.get('.top-menu-button').trigger('click');
    expect(wrapper.find('.search-form').exists()).toBe(false);
    await wrapper.get('.top-menu-search').trigger('click');
    await flushPromises();
    const input = wrapper.get('.top-menu-popup .search-form input');
    expect(document.activeElement).toBe(input.element);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);
    wrapper.unmount();
  });

  it('Ф41-6a: вкладки «Claude» нет; «Диспетчерская» — пункт меню «⋯», клик открывает страницу', async () => {
    stubFetch();
    store.state.projects = [project({ id: 1, name: 'Initech', unread: 3, unread_react: 0 })];
    const wrapper = mount(TopBar, { attachTo: document.body });
    expect(wrapper.find('.project-tab--claude').exists()).toBe(false);
    await wrapper.get('.top-menu-button').trigger('click');
    await wrapper.get('.dispatch-open').trigger('click');
    await flushPromises();
    expect(store.state.claudeOpen).toBe(true);
    expect(store.state.homeOpen).toBe(false);
    expect(wrapper.find('.top-menu-popup').exists()).toBe(false);
    store.state.claudeOpen = false;
    wrapper.unmount();
  });
});

// --- B3: вкладка помнит беседу, присутствие ------------------------------------------------------------------------

describe('Ф10-6 B3 (решение 4): вкладка проекта помнит беседу', () => {
  it('ушёл на главную и вернулся — та же беседа; скрытая или перенесённая — не открывается', async () => {
    const ch = channel({ id: 7, project_id: 3 });
    stubFetch({ 3: [ch], 4: [] });
    const local = createStore();
    await local.openProject(3);
    expect(local.state.currentChannelId).toBeNull();
    await local.openChannel(7);
    local.openHome();
    expect(local.state.currentChannelId).toBeNull();

    await local.openProject(3);
    await flushPromises();
    expect(local.state.currentChannelId).toBe(7);
    expect(local.state.homeOpen).toBe(false);

    // перенесли в другой проект — на вкладке 3 не открывается
    local.openHome();
    local.state.sync.channelsById[7] = { ...ch, project_id: 4 };
    stubFetch({ 3: [], 4: [] });
    await local.openProject(3);
    await flushPromises();
    expect(local.state.currentChannelId).toBeNull();

    // скрытая (удалена из хаба) — тоже нет
    local.state.sync.channelsById[7] = { ...ch, hidden: true };
    local.state.lastChannelByProject[3] = 7;
    local.openHome();
    await local.openProject(3);
    await flushPromises();
    expect(local.state.currentChannelId).toBeNull();

    // беседа ушла из стора вовсе
    delete local.state.sync.channelsById[7];
    local.openHome();
    await local.openProject(3);
    expect(local.state.currentChannelId).toBeNull();
  });

  it('у каждой вкладки своя беседа; поиск открывает найденную, а не запомненную', async () => {
    stubFetch({ 3: [channel({ id: 7, project_id: 3 }), channel({ id: 8, project_id: 3 })], 1: [channel({ id: 9, project_id: 1 })] });
    const local = createStore();
    await local.openProject(3);
    await local.openChannel(7);
    await local.openProject(1);
    await local.openChannel(9);
    await local.openProject(3);
    await flushPromises();
    expect(local.state.currentChannelId).toBe(7);
    await local.openProject(1);
    await flushPromises();
    expect(local.state.currentChannelId).toBe(9);
    local.openHome();
    await local.openSearchResult({ message_id: 1, channel_id: 8, project_id: 3 } as never);
    await flushPromises();
    expect(local.state.currentChannelId).toBe(8);
    expect(local.state.lastChannelByProject[3]).toBe(8);
  });
});

describe('Ф10-6 (решение 5): присутствие не заявляется, пока открыта главная', () => {
  it('presenceTarget = null при homeOpen, даже если беседа запомнена', () => {
    const local = createStore();
    const target = () => (local as unknown as { presenceTarget(): unknown }).presenceTarget();
    local.state.currentChannelId = 5;
    local.state.homeOpen = true;
    expect(target()).toBeNull();
    local.state.homeOpen = false;
    expect(target()).toEqual({ channelId: 5, threadExtId: null });
  });
});

// --- B3: half — тред поверх ленты, меню «⋯» -------------------------------------------------------------------------

describe('Ф10-6 B3: беседы в пол-экрана', () => {
  function openChannelWithThread(): void {
    store.state.projects = [project({ id: 3, name: 'Umbrella', workdir: '/w' })];
    store.state.sync.channelsById = { 7: channel({ id: 7, project_id: 3 }) };
    store.state.homeOpen = false;
    store.state.currentProjectId = 3;
    store.state.currentChannelId = 7;
    store.state.openThread = { channelId: 7, threadExtId: 'P1' };
    store.state.sync.threads['7:P1'] = { parent: null, replies: [], nextBefore: null, loading: false };
  }

  it('half: список | лента, тред — поверх правой части ленты; Esc закрывает, лента остаётся', async () => {
    stubFetch();
    initLayout(fakeMedia(960).matchMedia);
    openChannelWithThread();
    const wrapper = mount(ChatsView, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.classes()).toContain('chats-view--half');
    expect(wrapper.find('nav.column-channels').exists()).toBe(true);
    const thread = wrapper.get('.chats-main > .column-thread');
    expect(thread.classes()).toContain('column-thread--overlay');
    expect(wrapper.find('.chats-main > .column-feed').exists()).toBe(true);
    // клик по ленте вне панели её не закрывает
    await wrapper.get('.message-feed').trigger('click');
    expect(store.state.openThread).not.toBeNull();
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(store.state.openThread).toBeNull();
    expect(wrapper.find('.column-thread').exists()).toBe(false);
    expect(wrapper.find('.column-feed').exists()).toBe(true);
    wrapper.unmount();
  });

  it('full: тред — третьей колонкой (не поверх)', async () => {
    stubFetch();
    openChannelWithThread();
    const wrapper = mount(ChatsView);
    await flushPromises();
    expect(wrapper.classes()).toContain('chats-view--full');
    const thread = wrapper.get('.chats-view > .column-thread');
    expect(thread.classes()).not.toContain('column-thread--overlay');
    wrapper.unmount();
  });

  it('нет открытой беседы — «Выберите беседу.»', () => {
    stubFetch();
    store.state.homeOpen = false;
    store.state.currentProjectId = 3;
    const wrapper = mount(ChatsView);
    expect(wrapper.get('.placeholder-column').text()).toBe('Выберите беседу.');
    wrapper.unmount();
  });

  it('half и full: шапка — название, «⋯» с действиями', async () => {
    stubFetch();
    openChannelWithThread();
    initLayout(fakeMedia(960).matchMedia);
    const wrapper = mount(ChannelView, { props: { channelId: 7 }, attachTo: document.body });
    await flushPromises();
    const menuButton = wrapper.get('button.feed-menu-button');
    expect(menuButton.attributes('aria-expanded')).toBe('false');
    expect(wrapper.get('.feed-actions').isVisible()).toBe(false);
    await menuButton.trigger('click');
    expect(menuButton.attributes('aria-expanded')).toBe('true');
    const menu = wrapper.get('.feed-actions--menu');
    expect(menu.isVisible()).toBe(true);
    expect(menu.find('select.attention-select').exists()).toBe(true);
    expect(menu.get('button.mute-button').text()).toBe('Заглушить');
    expect(menu.get('button.hide-channel-button').text()).toBe('Удалить беседу из хаба');
    // действие закрывает меню
    await menu.get('button.mute-button').trigger('click');
    expect(menuButton.attributes('aria-expanded')).toBe('false');
    expect(wrapper.find('.back-button').exists()).toBe(false); // «‹» — только на телефоне

    // Ф90-3: шапка одной строкой и на полной ширине — действия тоже в «⋯».
    initLayout(null);
    await flushPromises();
    expect(wrapper.find('button.feed-menu-button').exists()).toBe(true);
    wrapper.unmount();
  });
});
