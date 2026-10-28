// Ф8-6, интерфейс (`docs/architecture.md`, тесты T5–T8 и поправки П2, П3): главная по
// умолчанию, «Нужна реакция» открывает беседу и подсвечивает сообщение, черновик — «Отправить» зовёт approve,
// «Открыть беседу» открывает и подсвечивает строку черновика, запуски Claude по проектам, группы «Реагирую /
// К сведению», черновики над полем ввода, «ждёт: N» ведёт на черновики. С Ф10-6 разделы главной — в столбцах
// доски (`37-board.spec.ts`): проверки общих списков переписаны на столбец проекта.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { AgentRun, Channel, HomeReactItem, Message, OutboxListItem, Project } from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelView from '../src/components/ChannelView.vue';
import DraftCard from '../src/components/DraftCard.vue';
import HomeView from '../src/components/HomeView.vue';
import MessageFeed from '../src/components/MessageFeed.vue';
import TopBar from '../src/components/TopBar.vue';
import { boardState, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createStore } from '../src/store/store';
import { buildLanes, laneStatusLine, startOfDay } from '../src/util/home';

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'c1',
    kind: 'dm',
    title: 'Покупатель',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 3,
    source: 'telegram',
    ...overrides,
  };
}

function project(overrides: Partial<Project>): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: null, unread: 0, ...overrides };
}

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
    project_id: 3,
    ext_id: '1',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'привет',
    entities_json: null,
    attachments_json: null,
    forward_json: null,
    mentions_me: false,
    refs_json: null,
    reactions_json: null,
    ts: 1000,
    edited_ts: null,
    deleted: false,
    outbox_id: null,
    outbox_status: null,
    sender: { id: 1, name: 'Пётр', handle: null },
    ...overrides,
  };
}

function draft(overrides: Partial<OutboxListItem>): OutboxListItem {
  return {
    id: 50,
    channel_id: 1,
    project_id: 3,
    channel_title: 'Покупатель',
    text: 'Добрый день!\nПосмотрю сегодня.',
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: 2000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Umbrella',
    source: 'telegram',
    kind: 'dm',
    token_name: 'mcp:wake-3',
    author: 'Claude · Umbrella',
    reply_to: { ext_id: '5', sender: 'Пётр', excerpt: 'Когда подпишете?' },
    thread: null,
    run_id: 7,
    ...overrides,
  };
}

function run(overrides: Partial<AgentRun>): AgentRun {
  return {
    id: 7,
    project_id: 3,
    project_name: 'Umbrella',
    project_color: '#ff0000',
    kind: 'urgent',
    status: 'ok',
    started_ts: 1000,
    finished_ts: 2000,
    card_ids: [1],
    channels: [{ id: 1, title: 'Покупатель' }],
    channel_titles: ['Покупатель'],
    draft_ids: [50],
    result: 'Положил черновик.\nВторая строка.',
    failure: null,
    session_id: 's',
    num_turns: 3,
    duration_ms: 1000,
    ...overrides,
  };
}

function reactItem(overrides: Partial<HomeReactItem> = {}): HomeReactItem {
  return {
    channel: channel({ unread_react: 2 }),
    project: { id: 3, name: 'Umbrella', color: '#ff0000' },
    last: { sender: 'Пётр', excerpt: 'Когда подпишете?', ts: 1000, message_id: 11 },
    card: null,
    draft_count: 1,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  query: string;
  body: unknown;
}

interface Fixtures {
  react?: HomeReactItem[];
  drafts?: OutboxListItem[];
  runs?: AgentRun[];
  sent?: OutboxListItem[];
  messages?: Message[];
}

function homeFetch(fx: Fixtures = {}): { sent: Sent[]; fetchMock: ReturnType<typeof vi.fn> } {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    sent.push({
      method,
      path: url.pathname,
      query: url.search,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    const p = url.pathname;
    if (p === '/api/v1/home/react') return jsonResponse(fx.react ?? []);
    if (p === '/api/v1/work/tasks') return jsonResponse([]);
    if (p === '/api/v1/agent/runs') return jsonResponse(fx.runs ?? []);
    if (p === '/api/v1/outbox') {
      return jsonResponse(url.searchParams.get('status') === 'sent' ? (fx.sent ?? []) : (fx.drafts ?? []));
    }
    if (p === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (p === '/api/v1/status') {
      return jsonResponse({ accounts: [], dead_letter: {}, outbox_pending: 0, outbox_unknown: 0, max_seq: 1, now: 0 });
    }
    if (p === '/api/v1/projects') return jsonResponse([]);
    if (p.startsWith('/api/v1/projects/') && p.endsWith('/channels')) return jsonResponse([]);
    if (p.startsWith('/api/v1/channels/') && p.endsWith('/messages')) {
      return jsonResponse({ messages: fx.messages ?? [], next_before: null });
    }
    return jsonResponse({ status: 'queued' });
  });
  return { sent, fetchMock };
}

function resetStore(): void {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  store.state.projects = [];
  store.state.currentProjectId = null;
  store.state.currentChannelId = null;
  store.state.homeOpen = true;
  store.state.homeAnchor = null;
  store.state.focus = null;
  store.state.home = { react: [], drafts: [], runs: [], sent: [], loadedAt: null, error: null };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetStore();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// --- T5: главная по умолчанию ----------------------------------------------------------------------------------

describe('Ф8-6 T5: главная — открыта по умолчанию', () => {
  // Ф10-6: «Главная — первый пункт слева» (MainApp + ProjectList) — теперь вкладки оболочки, проверяются в
  // `38-shell.spec.ts` (исполнитель B). Здесь — загрузка главной и раскладка доски.
  it('новый стор открывается на главной; главная грузит все разделы и показывает доску без оркестратора', async () => {
    expect(createStore().state.homeOpen).toBe(true);
    const { sent, fetchMock } = homeFetch({ react: [reactItem()] });
    vi.stubGlobal('fetch', fetchMock);
    store.state.projects = [project({ workdir: '/w' })];
    const wrapper = mount(HomeView);
    await flushPromises();
    expect(wrapper.find('main.home-view').exists()).toBe(true);
    // все четыре раздела данных загружены; «Отправлено сегодня» и запуски — с начала дня
    const paths = sent.map((r) => r.path + r.query);
    expect(paths).toContain('/api/v1/home/react');
    expect(paths.some((x) => x.startsWith('/api/v1/agent/runs?since='))).toBe(true);
    expect(paths.some((x) => x.includes('status=sent') && x.includes('since='))).toBe(true);
    // Ф20-4: чата оркестратора на главной нет (он на вкладке «Claude»), только доска; в столбце проекта —
    // непустые разделы сверху вниз и серая строка про Claude
    const children = [...wrapper.get('main.home-view').element.children].map((e) => e.className.split(' ')[0]);
    expect(children).toEqual(['board']);
    const sections = wrapper.findAll('.board-column[data-project-id="3"] [data-section]');
    expect(sections.map((s) => s.attributes('data-section'))).toEqual(['react', 'claude']);
    expect(wrapper.get('[data-section="claude"]').text()).toContain('сессия: свободна');
    // имя проекта в шапке столбца уводит с главной на вкладку проекта
    await wrapper.get('.board-column-name').trigger('click');
    await flushPromises();
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.currentProjectId).toBe(3);
    wrapper.unmount();
  });

  it('startOfDay — местная полночь', () => {
    const now = new Date(2026, 8, 24, 15, 30).getTime();
    expect(startOfDay(now)).toBe(new Date(2026, 8, 24, 0, 0).getTime());
  });
});

// --- T6: «Нужна реакция» ------------------------------------------------------------------------------------

describe('Ф8-6 T6: клик в «Нужна реакция» открывает беседу и прокручивает к сообщению', () => {
  it('строка в столбце проекта: беседа, источник, число, «отправитель: превью», «черновик»; клик — проект, канал, фокус', async () => {
    const { fetchMock } = homeFetch({ react: [reactItem()] });
    vi.stubGlobal('fetch', fetchMock);
    store.state.projects = [project({})];
    const wrapper = mount(HomeView);
    await flushPromises();
    const row = wrapper.get('.board-column[data-project-id="3"] .home-react-row');
    expect(row.get('.home-react-address').text()).toBe('Покупатель');
    expect(row.text()).toContain('Telegram');
    expect(row.get('.badge-react').text()).toBe('2');
    expect(row.get('.home-react-last').text()).toBe('Пётр: Когда подпишете?');
    expect(row.get('.home-react-draft').text()).toBe('черновик');
    resetBoard();
    await row.trigger('click');
    await flushPromises();
    // На «Главной» компьютера беседа открывается окном, экран и текущая беседа не меняются
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.currentChannelId).toBeNull();
    expect(store.state.channelModalId).toBe(1);
    expect(boardState.layout.open).toEqual([]);
    expect(store.state.focus).toMatchObject({ channelId: 1, messageId: 11, outboxId: null });
    wrapper.unmount();
  });

  it('лента прокручивает к цели фокуса и подсвечивает её строку; фокус снимается', async () => {
    const { fetchMock } = homeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const scrolled: number[] = [];
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(Number(this.getAttribute('data-message-id')));
    };
    store.state.sync.messagesByChannel[1] = [msg({ id: 10, ext_id: '4' }), msg({ id: 11, ext_id: '5', ts: 1100 })];
    store.state.focus = { channelId: 1, messageId: 11, outboxId: null, seq: 1 };
    const wrapper = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    await flushPromises();
    expect(scrolled).toContain(11);
    expect(wrapper.get('[data-message-id="11"]').classes()).toContain('message-row--focus');
    expect(wrapper.get('[data-message-id="10"]').classes()).not.toContain('message-row--focus');
    expect(store.state.focus).toBeNull();
    wrapper.unmount();
  });
});

// --- T7: черновик ----------------------------------------------------------------------------------------------

describe('Ф8-6 T7: черновик на главной', () => {
  it('адрес, «ответ на», автор; «Отправить» зовёт approve; «Править» — поле на месте, отправка с правкой', async () => {
    const { sent, fetchMock } = homeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(DraftCard, { props: { item: draft({}) } });
    expect(wrapper.get('.draft-card-address').text()).toBe('Umbrella › Покупатель');
    expect(wrapper.get('.draft-card-target').text()).toBe('ответ на: Пётр: Когда подпишете?');
    expect(wrapper.get('.draft-card-author').text()).toBe('Claude · Umbrella');
    expect(wrapper.get('.draft-card-text').text()).toContain('Посмотрю сегодня.');
    await wrapper.get('button.draft-send').trigger('click');
    await flushPromises();
    const approves = () => sent.filter((r) => r.method === 'POST' && r.path === '/api/v1/outbox/50/approve');
    expect(approves()).toHaveLength(1);
    expect(approves()[0]!.body).toEqual({ text: null });

    await wrapper.get('button.draft-edit').trigger('click');
    const area = wrapper.get('textarea.draft-card-edit');
    await area.setValue('Исправленный ответ');
    await wrapper.get('button.draft-send').trigger('click');
    await flushPromises();
    expect(approves()[1]!.body).toEqual({ text: 'Исправленный ответ' });
    wrapper.unmount();
  });

  it('«в тред: …» для ответа в треде; «Отклонить» зовёт cancel и убирает черновик из списка', async () => {
    const { sent, fetchMock } = homeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const item = draft({ id: 51, reply_to: null, thread: { ext_id: '100', excerpt: 'Родитель треда' } });
    store.state.home.drafts = [item];
    const wrapper = mount(DraftCard, { props: { item } });
    expect(wrapper.get('.draft-card-target').text()).toBe('в тред: Родитель треда');
    await wrapper.get('button.draft-reject').trigger('click');
    await flushPromises();
    expect(sent.some((r) => r.method === 'POST' && r.path === '/api/v1/outbox/51/cancel')).toBe(true);
    expect(store.state.home.drafts).toEqual([]);
    wrapper.unmount();
  });

  it('«Открыть беседу» открывает проект и беседу и подсвечивает строку черновика в ленте', async () => {
    const draftRow = msg({ id: 77, ext_id: null, is_me: true, outbox_id: 50, outbox_status: 'pending_approval' });
    const { fetchMock } = homeFetch({ messages: [msg({ id: 76 }), draftRow] });
    vi.stubGlobal('fetch', fetchMock);
    Element.prototype.scrollIntoView = function () {};
    const card = mount(DraftCard, { props: { item: draft({}) } });
    resetBoard();
    await card.get('button.draft-open').trigger('click');
    await flushPromises();
    expect(store.state.currentChannelId).toBeNull(); // окно беседы, а не экран беседы
    expect(store.state.channelModalId).toBe(1);
    expect(boardState.layout.open).toEqual([]);
    expect(store.state.focus).toMatchObject({ channelId: 1, outboxId: 50 });
    const feed = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    await flushPromises();
    expect(feed.get('[data-message-id="77"]').classes()).toContain('message-row--focus');
    expect(store.state.focus).toBeNull();
    feed.unmount();
    card.unmount();
  });
});

// --- C2: черновик Claude над полем ввода ----------------------------------------------------------------------

describe('Ф8-6 C2: черновики беседы закреплены над полем ввода', () => {
  it('все черновики беседы по порядку, с «Черновик Claude:» и действиями; чужие беседы — нет', async () => {
    const { fetchMock } = homeFetch({
      drafts: [
        draft({ id: 52, created_ts: 3000, text: 'второй' }),
        draft({ id: 50, created_ts: 2000, text: 'первый' }),
        draft({ id: 60, channel_id: 2, text: 'из другой беседы' }),
      ],
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.sync.channelsById[1] = channel({});
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    const pinned = wrapper.findAll('.pinned-drafts .draft-card');
    expect(pinned.map((c) => c.get('.draft-card-text').text())).toEqual(['первый', 'второй']);
    expect(pinned[0]!.get('.draft-card-label').text()).toBe('Черновик Claude:');
    expect(pinned[0]!.findAll('button').map((b) => b.text())).toEqual(['Отправить', 'Править', 'Отклонить']);
    // поле ввода на месте, под черновиками
    expect(wrapper.find('form.composer').exists()).toBe(true);
    wrapper.unmount();
  });
});

// --- П2: «Claude по проектам» дорожками ----------------------------------------------------------------------

describe('Ф8-6 П2: «Claude по проектам» — дорожка на проект', () => {
  it('buildLanes: проект с running — первым, запуски под своими дорожками, новые сверху; без запусков — «не будился»', () => {
    const runs = [
      run({ id: 1, project_id: 3, started_ts: 5000, status: 'ok' }),
      run({ id: 2, project_id: 1, project_name: 'Globex', started_ts: 1000, status: 'running' }),
      run({ id: 3, project_id: 3, started_ts: 6000, status: 'failed' }),
      run({ id: 4, project_id: 1, project_name: 'Globex', started_ts: 500, status: 'ok' }),
    ];
    const projects = [
      project({ id: 1, name: 'Globex', workdir: '/w/m' }),
      project({ id: 3, workdir: '/w/i' }),
      project({ id: 2, name: 'Initech', workdir: '/w/n' }),
      project({ id: 4, name: 'Без папки', workdir: null }),
    ];
    const { lanes, idle } = buildLanes(runs, projects);
    expect(lanes.map((l) => l.projectId)).toEqual([1, 3]);
    expect(lanes[0]!.runs.map((r) => r.id)).toEqual([2, 4]);
    expect(lanes[1]!.runs.map((r) => r.id)).toEqual([3, 1]);
    expect(lanes[0]!.running?.id).toBe(2);
    expect(idle.map((p) => p.name)).toEqual(['Initech']);
    expect(laneStatusLine(lanes[0]!, 1000 + 7 * 60_000)).toBe('работает 7 мин: срочное, Покупатель');
    expect(laneStatusLine(lanes[1]!, 0)).toMatch(/^свободна · последний запуск \d{2}:\d{2}$/);
  });

  it('столбец главной: серая строка «Claude сегодня: K · сессия: … · Claude ›» только у проекта с папкой; ссылка открывает вкладку «Claude»', async () => {
    const { fetchMock } = homeFetch({ runs: [run({ id: 1, project_id: 3, started_ts: 5000, status: 'ok' })] });
    vi.stubGlobal('fetch', fetchMock);
    store.state.projects = [project({ id: 3, workdir: '/w' }), project({ id: 4, name: 'Личное', workdir: null })];
    const wrapper = mount(HomeView);
    await flushPromises();
    expect(wrapper.get('.board-column[data-project-id="3"] .board-claude-line').text()).toMatch(
      /^Claude сегодня: 1 · сессия: свободна ·\s+Агенты ›$/,
    );
    expect(wrapper.find('.board-column[data-project-id="4"] .board-claude-line').exists()).toBe(false);
    await wrapper.get('.board-claude-link').trigger('click');
    await flushPromises();
    expect(store.state.claudeOpen).toBe(true);
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.boardFocus?.projectId).toBe(3);
    store.state.claudeOpen = false;
    wrapper.unmount();
  });
});

// --- «Отправлено сегодня» -------------------------------------------------------------------------------------

describe('Ф8-6 C1: «Отправлено сегодня»', () => {
  it('раздел свёрнут; время, беседа, «через черновик Claude» / «вручную», текст свёрнут и разворачивается кликом', async () => {
    const { fetchMock } = homeFetch({
      sent: [
        draft({ id: 1, status: 'sent', sent_ts: 5000 }),
        draft({ id: 2, status: 'sent', sent_ts: 4000, origin: 'ui', author: 'ты', token_name: 'ui' }),
      ],
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.projects = [project({})];
    const wrapper = mount(HomeView);
    await flushPromises();
    // Ф10-6: «Отправлено сегодня · N» свёрнуто в столбце проекта
    const section = wrapper.get('.board-column[data-project-id="3"] [data-section="sent"]');
    expect(section.findAll('.home-sent-row')).toHaveLength(0);
    await section.get('.board-section-toggle').trigger('click');
    expect(section.get('.board-section-toggle').text()).toBe('Отправлено сегодня · 2');
    const rows = section.findAll('.home-sent-row');
    expect(rows.map((r) => r.get('.home-sent-via').text())).toEqual(['через черновик Claude', 'вручную']);
    expect(rows[0]!.get('.home-sent-address').text()).toBe('Покупатель');
    const text = rows[0]!.get('.home-sent-text');
    expect(text.classes()).not.toContain('home-sent-text--open');
    await text.trigger('click');
    expect(text.classes()).toContain('home-sent-text--open');
    wrapper.unmount();
  });
});

// --- T8: группы «Реагирую / К сведению» -----------------------------------------------------------------------

describe('Ф8-6 T8: список бесед проекта — «Реагирую» и «К сведению»', () => {
  it('react и unread_react > 0 — в «Реагирую»; остальное — в свёрнутой «К сведению» со счётчиком', async () => {
    store.state.currentProjectId = 3;
    store.state.sync.channelsById = {
      1: channel({ id: 1, title: 'личка', attention: 'react' }),
      2: channel({ id: 2, kind: 'group', title: 'команда', attention: 'info', unread_react: 1, unread: 3 }),
      3: channel({ id: 3, kind: 'group', title: 'флуд', attention: 'info', unread: 4 }),
      4: channel({ id: 4, kind: 'mail_thread', title: 'письмо', attention: 'info', unread: 1 }),
      5: channel({ id: 5, kind: 'group', title: 'разбор', attention: 'info' }),
    };
    const wrapper = mount(ChannelList);
    const react = wrapper.get('.attention-group--react');
    expect(react.get('.attention-group-title').text()).toBe('Реагирую');
    expect(react.findAll('.channel-title').map((t) => t.text()).sort()).toEqual(['команда', 'личка']);
    const info = wrapper.get('.attention-group--info');
    const toggle = info.get('.attention-group-toggle');
    expect(toggle.text()).toContain('К сведению');
    expect(toggle.get('.badge').text()).toBe('5');
    expect(toggle.attributes('aria-expanded')).toBe('false');
    expect(info.findAll('.channel-title')).toHaveLength(0);
    await toggle.trigger('click');
    expect(info.findAll('.channel-title').map((t) => t.text()).sort()).toEqual(['письмо', 'разбор', 'флуд']);
    // «Чаты / Почта / Задачи» — внутри группы
    expect(info.findAll('.channel-group-title').map((h) => h.text())).toEqual(['Чаты', 'Почта']);
    wrapper.unmount();
  });

  it('открытая беседа «к сведению» разворачивает группу', () => {
    store.state.currentProjectId = 3;
    store.state.currentChannelId = 3;
    store.state.sync.channelsById = { 3: channel({ id: 3, kind: 'group', title: 'флуд', attention: 'info' }) };
    const wrapper = mount(ChannelList);
    expect(wrapper.get('.attention-group--info').findAll('.channel-title').map((t) => t.text())).toEqual(['флуд']);
    wrapper.unmount();
  });
});

// --- C4: «ждёт: N» ведёт на черновики -------------------------------------------------------------------------

describe('Ф8-6 C4: кнопка «ждёт: N» — раздел «Черновики» главной', () => {
  it('клик открывает главную с якорем «drafts»', async () => {
    const { fetchMock } = homeFetch();
    vi.stubGlobal('fetch', fetchMock);
    store.state.homeOpen = false;
    store.state.currentProjectId = 3;
    // Ф10-6 (решение 6): счётчик «ждёт: N» виден только при N > 0
    store.state.status = {
      accounts: [],
      dead_letter: {},
      outbox_pending: 2,
      outbox_approval: 2,
      outbox_unknown: 0,
      max_seq: 1,
      now: 0,
    };
    const wrapper = mount(TopBar);
    await wrapper.get('.top-menu-button').trigger('click'); // Ф20-4: «ждёт: N» — в меню «⋯»
    const button = wrapper.findAll('button').find((b) => b.text().startsWith('ждёт:'));
    await button!.trigger('click');
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.homeAnchor).toBe('drafts');
    expect(store.state.currentProjectId).toBeNull();
    wrapper.unmount();
    store.state.status = null;
  });
});
