// Ф11-4 (`docs/architecture.md`): хвосты интерфейса. И2 — шапка сообщения в узкой панели треда
// (имя многоточием, время без переноса, «из треда» целиком); И3б — «Стоп» в свёрнутом оркестраторе; И4 —
// «Отправлено сегодня» в столбце «Неразобранное»; И5а — доска -> проект при открытой беседе на телефоне не растит
// историю; И5б — подсказки Ctrl+Enter нет при `hover: none` без `pointer: coarse`; И6 — пауза отправки Telegram
// (`flood_until`) в полосе, пропадает по времени. jsdom не считает раскладку — стили проверяются по правилам CSS.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type {
  Account,
  Channel,
  Message,
  OutboxListItem,
  Project,
  StatusResponse,
} from '../src/api/types';
import MainApp from '../src/components/MainApp.vue';
import MessageRow from '../src/components/MessageRow.vue';
import Composer from '../src/components/Composer.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';
import { buildBoard } from '../src/util/board';
import { formatTime } from '../src/util/home';
import { initLayout } from '../src/util/layout';
import { isTouchScreen } from '../src/util/submitKeys';
import { fakeMedia } from './media';

const NOW = new Date(2026, 8, 26, 12, 0, 0).getTime();

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

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

function sent(id: number, projectId: number | null, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: id * 10,
    project_id: projectId,
    channel_title: `беседа ${id}`,
    text: `ответ ${id}`,
    status: 'sent',
    origin: 'ui',
    error: null,
    attempts: 1,
    created_ts: NOW - 60_000,
    sent_ts: NOW - 30_000,
    ext_id: `e${id}`,
    ...overrides,
  };
}

/** Все объявления правил `css`, в списке селекторов которых есть `selector`, одной строкой. */
function rule(css: string, selector: string): string {
  const bodies: string[] = [];
  for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
    const selectors = m[1]!.replace(/\/\*[\s\S]*?\*\//g, '').split(',').map((x) => x.trim());
    if (selectors.includes(selector)) bodies.push(m[2]!);
  }
  expect(bodies.length, selector).toBeGreaterThan(0);
  return bodies.join('\n');
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --- И2 ---------------------------------------------------------------------------------------------------------

describe('Ф11-4 И2: шапка сообщения в узкой панели', () => {
  it('имя — многоточием, время — без переноса и цифрами одной ширины, «из треда» — целиком', () => {
    const message: Message = {
      id: 1,
      seq: 1,
      first_seq: 1,
      channel_id: 1,
      project_id: 1,
      ext_id: '1',
      thread_ext_id: 'P1',
      thread_broadcast: true,
      purged: false,
      thread: null,
      reply_to_ext_id: null,
      grouped_id: null,
      is_me: false,
      kind: 'text',
      text: 'ответ в треде',
      entities_json: null,
      attachments_json: null,
      forward_json: null,
      mentions_me: false,
      refs_json: null,
      reactions_json: null,
      ts: NOW,
      edited_ts: null,
      deleted: false,
      outbox_id: null,
      outbox_status: null,
      sender: { id: 1, name: 'Анастасия Длиннофамильная-Двойная', handle: null },
    };
    store.state.sync.channelsById = {};
    const wrapper = mount(MessageRow, { props: { message } });
    const meta = wrapper.get('.message-meta');
    expect(meta.get('.sender-name').text()).toBe('Анастасия Длиннофамильная-Двойная');
    expect(meta.get('.message-ts').element.tagName).toBe('TIME');
    expect(meta.get('.thread-broadcast-tag').text()).toBe('из треда');

    const css = readFileSync(resolve(__dirname, '../src/styles/layout.css'), 'utf8');
    expect(rule(css, '.message-meta')).toMatch(/flex-wrap: wrap/);
    const name = rule(css, '.message-meta .sender-name');
    expect(name).toMatch(/text-overflow: ellipsis/);
    expect(name).toMatch(/white-space: nowrap/);
    expect(name).toMatch(/overflow: hidden/);
    expect(name).toMatch(/min-width: 0/);
    const time = rule(css, '.message-meta .message-ts');
    expect(time).toMatch(/white-space: nowrap/);
    expect(time).toMatch(/font-variant-numeric: tabular-nums/);
    expect(time).toMatch(/flex: none/);
    expect(rule(css, '.message-meta > .thread-broadcast-tag')).toMatch(/white-space: nowrap/);
    wrapper.unmount();
  });
});

// --- И4 ---------------------------------------------------------------------------------------------------------

describe('Ф11-4 И4: «Отправлено сегодня» в столбце «Неразобранное»', () => {
  it('отправленное из бесед без проекта — в «Неразобранном» (столбец есть и без реакции), тем же разделом', async () => {
    const home = { react: [], drafts: [], runs: [], sent: [sent(1, null), sent(2, 1), sent(3, -1)] };
    const board = buildBoard([project({ id: 1 })], home, NOW);
    expect(board.columns[0]!.sent.map((s) => s.id)).toEqual([2]);
    expect(board.unsorted?.sent.map((s) => s.id)).toEqual([1, 3]);
    expect(board.unsorted?.waitCount).toBe(0);

    const wrapper = mount(ProjectColumn, { props: { column: board.unsorted! } });
    const section = wrapper.get('[data-section="sent"]');
    const toggle = section.get('button.board-section-toggle');
    expect(toggle.text()).toBe('Отправлено сегодня · 2');
    await toggle.trigger('click');
    const rows = wrapper.findAll('.home-sent-row');
    expect(rows.map((r) => r.get('.home-sent-address').text())).toEqual(['беседа 1', 'беседа 3']);
    expect(rows[0]!.get('.home-sent-text').text()).toBe('ответ 1');
    // разделов проекта у «Неразобранного» по-прежнему нет
    expect(wrapper.find('[data-section="claude"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

// --- И5 ---------------------------------------------------------------------------------------------------------

describe('Ф11-4 И5а: доска -> проект при открытой беседе на телефоне', () => {
  beforeEach(() => {
    api.setCsrf('c');
    api.setUnauthorizedHandler(null);
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
    window.history.replaceState(null, '');
    initLayout(fakeMedia(390).matchMedia);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        const url = new URL(input, 'http://localhost');
        if (/^\/api\/v1\/projects\/-?\d+\/channels$/.test(url.pathname)) return jsonResponse([]);
        if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
        if (url.pathname === '/api/v1/status') {
          return jsonResponse({ accounts: [], dead_letter: {}, outbox_pending: 0, outbox_approval: 0, outbox_unknown: 0, max_seq: 1, now: 0 });
        }
        return jsonResponse([]);
      }),
    );
    store.state.projects = [project({ id: 1 }), project({ id: 2, name: 'Globex' })];
    store.state.sync.channelsById = { 7: channel({ id: 7 }) };
  });

  afterEach(() => {
    initLayout(null);
  });

  it('проект с запомненной беседой: запись истории беседы занимается заново, новой нет', async () => {
    const wrapper = mount(MainApp, { attachTo: document.body });
    await flushPromises();
    const pushState = vi.spyOn(window.history, 'pushState');
    await store.openProject(1);
    await store.openChannel(7);
    await flushPromises();
    expect(pushState).toHaveBeenCalledTimes(1);
    const length = window.history.length;

    store.setPhoneTab('projects'); // доска; беседа остаётся выбранной
    await flushPromises();
    await store.openProject(1); // имя столбца на доске
    await flushPromises();
    expect(store.state.currentChannelId).toBe(7);
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(window.history.length).toBe(length);
    wrapper.unmount();
  });

  it('проект без запомненной беседы: осиротевшая запись снимается одним history.back()', async () => {
    const wrapper = mount(MainApp, { attachTo: document.body });
    await flushPromises();
    const pushState = vi.spyOn(window.history, 'pushState');
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => {});
    await store.openProject(1);
    await store.openChannel(7);
    await flushPromises();
    store.setPhoneTab('projects');
    await store.openProject(2);
    await flushPromises();
    expect(store.state.currentChannelId).toBeNull();
    expect(pushState).toHaveBeenCalledTimes(1);
    expect(back).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });
});

describe('Ф11-4 И5б: подсказка Ctrl+Enter на телефоне', () => {
  function stubMedia(matching: string[]): void {
    vi.stubGlobal('matchMedia', (query: string) => ({ media: query, matches: matching.includes(query) }));
  }

  it('`hover: none` без `pointer: coarse` — подсказки нет; обычный экран — есть', () => {
    stubMedia(['(hover: none)']);
    expect(isTouchScreen()).toBe(true);
    const touch = mount(Composer, { props: { channelId: 1 } });
    expect(touch.find('.submit-hint').exists()).toBe(false);
    touch.unmount();

    stubMedia([]);
    expect(isTouchScreen()).toBe(false);
    const desktop = mount(Composer, { props: { channelId: 1 } });
    expect(desktop.get('.composer-send').attributes('title')).toMatch(/Enter/);
    desktop.unmount();
  });

  it('браузер молчит про указатель, но экран сенсорный (`maxTouchPoints > 0`) — телефон', () => {
    stubMedia([]);
    // в jsdom свойства нет — задаём на время теста
    Object.defineProperty(navigator, 'maxTouchPoints', { value: 5, configurable: true });
    try {
      expect(isTouchScreen()).toBe(true);
    } finally {
      delete (navigator as unknown as { maxTouchPoints?: number }).maxTouchPoints;
    }
    expect(isTouchScreen()).toBe(false);
  });
});

// --- И6 ---------------------------------------------------------------------------------------------------------

describe('Ф11-4 И6: пауза отправки Telegram в полосе', () => {
  function account(overrides: Partial<Account>): Account {
    return {
      id: 1,
      source: 'telegram',
      title: 'Телеграм',
      status: 'ok',
      last_ok_ts: null,
      last_error: null,
      flood_until: null,
      ...overrides,
    };
  }

  function status(accounts: Account[]): StatusResponse {
    return { accounts, dead_letter: {}, outbox_pending: 0, outbox_approval: 0, outbox_unknown: 0, max_seq: 1, now: NOW };
  }

  it('`flood_until` через 10 мин — жёлтая пометка с временем; время прошло — пометки нет', async () => {
    vi.useFakeTimers({ now: NOW });
    const until = NOW + 10 * 60_000;
    store.state.status = status([account({ flood_until: until }), account({ id: 2, source: 'slack', title: 'Globex' })]);
    const wrapper = mount(TopBar);
    const items = () => wrapper.findAll('ul.account-statuses li');
    expect(items().map((li) => li.text())).toEqual([`Телеграм: пауза отправки до ${formatTime(until)}`]);
    expect(items()[0]!.classes()).toContain('status-stale');

    vi.advanceTimersByTime(11 * 60_000);
    await flushPromises();
    // Ф20-4: пометки в полосе нет, «аккаунты ✓» — только в меню «⋯»
    expect(items()).toEqual([]);
    await wrapper.get('.top-menu-button').trigger('click');
    expect(wrapper.get('.accounts-ok').text()).toBe('аккаунты ✓');
    await wrapper.get('.top-menu-button').trigger('click');

    // пауза в прошлом с самого начала — пометки нет
    store.state.status = status([account({ flood_until: Date.now() - 1000 })]);
    await flushPromises();
    expect(items()).toEqual([]);
    wrapper.unmount();
  });
});
