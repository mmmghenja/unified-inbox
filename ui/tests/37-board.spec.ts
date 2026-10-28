// Ф10-6 A (`docs/architecture.md`): главная-доска — `buildBoard` (раскладка по проектам,
// «Неразобранное», порядок столбцов, waitCount), столбцы и их разделы, якорь «ждёт: N», оркестратор (свернуть,
// запоминание выбора, недоступное хранилище, `collapsible=false`), телефонная доска с чипами.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type {
  AgentRun,
  Channel,
  HomeReactItem,
  OutboxListItem,
  Project,
} from '../src/api/types';
import HomeView from '../src/components/HomeView.vue';
import { store } from '../src/store/instance';
import { buildBoard } from '../src/util/board';
import { startOfDay } from '../src/util/home';

const NOW = new Date(2026, 8, 25, 12, 0, 0).getTime();
const DAY = startOfDay(NOW);

function project(overrides: Partial<Project>): Project {
  return { id: 1, name: 'Globex', color: '#ff8800', sort: 0, muted: false, workdir: '/w/m', unread: 0, ...overrides };
}

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
    unread: 1,
    project_id: 1,
    source: 'telegram',
    ...overrides,
  };
}

function react(projectId: number, channelId: number, overrides: Partial<HomeReactItem> = {}): HomeReactItem {
  return {
    channel: channel({ id: channelId, project_id: projectId, title: `беседа ${channelId}` }),
    project: { id: projectId, name: `п${projectId}`, color: null },
    last: { sender: 'Пётр', excerpt: 'Когда?', ts: NOW - 60_000, message_id: channelId * 10 },
    card: null,
    draft_count: 0,
    ...overrides,
  };
}

function outbox(id: number, projectId: number | null, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: 1,
    project_id: projectId,
    channel_title: 'Покупатель',
    text: `текст ${id}`,
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: NOW - id * 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Globex',
    source: 'telegram',
    kind: 'dm',
    author: 'Claude · Globex',
    reply_to: null,
    thread: null,
    ...overrides,
  };
}

function run(id: number, projectId: number, overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id,
    project_id: projectId,
    project_name: null,
    project_color: null,
    kind: 'reply',
    status: 'ok',
    started_ts: NOW - 3_600_000 + id,
    finished_ts: null,
    card_ids: [],
    channels: [],
    channel_titles: [],
    draft_ids: [],
    result: null,
    failure: null,
    session_id: null,
    num_turns: null,
    duration_ms: null,
    ...overrides,
  };
}

// --- A1: buildBoard ---------------------------------------------------------------------------------------------

describe('Ф10-6 A1: buildBoard', () => {
  afterEach(() => vi.restoreAllMocks());

  it('раскладывает реакцию, черновики, запуски, отправленное и дорожки по проектам; порядок внутри разделов', () => {
    const projects = [project({ id: 1 }), project({ id: 2, name: 'Initech' }), project({ id: -1, name: 'Неразобранное' })];
    const home = {
      react: [react(2, 21), react(1, 11), react(2, 22)],
      drafts: [outbox(5, 1, { created_ts: 5000 }), outbox(3, 1, { created_ts: 3000 }), outbox(4, 2)],
      runs: [
        run(1, 1, { started_ts: 1000 }),
        run(2, 1, { started_ts: 3000, status: 'running' }),
        run(3, 2, { started_ts: 2000 }),
      ],
      sent: [outbox(30, 2, { status: 'sent' }), outbox(31, 1, { status: 'sent' }), outbox(32, 2, { status: 'sent' })],
    };
    const { columns, unsorted } = buildBoard(projects, home, NOW);
    expect(columns.map((c) => c.project.id)).toEqual([1, 2]); // псевдо-проекты — не столбцы
    const [a, b] = columns;
    expect(a!.react.map((r) => r.channel.id)).toEqual([11]);
    expect(b!.react.map((r) => r.channel.id)).toEqual([21, 22]); // порядок API
    expect(a!.drafts.map((d) => d.id)).toEqual([3, 5]); // старые сверху
    expect(b!.drafts.map((d) => d.id)).toEqual([4]);
    expect(a!.runs.map((r) => r.id)).toEqual([2, 1]); // новые сверху
    expect(a!.running?.id).toBe(2);
    expect(b!.running).toBeNull();
    expect(b!.sent.map((s) => s.id)).toEqual([30, 32]); // порядок API
    expect(a!.sent.map((s) => s.id)).toEqual([31]);
    expect(b!.state).toEqual({ line: 'свободна', wait: false });
    expect(a!.state).toEqual({ line: 'свободна', wait: false });
    expect(unsorted).toBeNull();
  });

  it('проект null, -1 или не из списка — в «Неразобранное» (реакция и черновики); запуски и отправленное — пропуск с warn', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const projects = [project({ id: 1 })];
    const home = {
      react: [react(-1, 91), react(9, 92), react(1, 11)],
      drafts: [outbox(1, null), outbox(2, 9), outbox(3, 1)],
      runs: [run(1, 9)],
      sent: [outbox(40, 9, { status: 'sent' })],
    };
    const { columns, unsorted } = buildBoard(projects, home, NOW);
    expect(columns[0]!.react.map((r) => r.channel.id)).toEqual([11]);
    expect(columns[0]!.drafts.map((d) => d.id)).toEqual([3]);
    expect(unsorted?.react.map((r) => r.channel.id)).toEqual([91, 92]);
    expect(unsorted?.drafts.map((d) => d.id)).toEqual([2, 1]); // старые сверху
    expect(unsorted?.waitCount).toBe(2);
    expect(columns[0]!.runs).toEqual([]);
    expect(warn).toHaveBeenCalledTimes(2); // запуск 1, отправленное 40
  });

  it('проекты ещё не загружены — пропуск без warn (первые кадры после входа)', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const home = { react: [], drafts: [], runs: [run(1, 9)], sent: [outbox(40, 9, { status: 'sent' })] };
    expect(buildBoard([], home, NOW).columns).toEqual([]);
    expect(warn).not.toHaveBeenCalled();
  });

  it('«Неразобранное» — null, пока там нет ни реакции, ни черновиков', () => {
    const empty = { react: [], drafts: [], runs: [], sent: [] };
    expect(buildBoard([project({})], empty, NOW).unsorted).toBeNull();
    const onlyReact = { ...empty, react: [react(-1, 91)] };
    expect(buildBoard([project({})], onlyReact, NOW).unsorted?.react).toHaveLength(1);
  });

  it('порядок столбцов — порядок проектов', () => {
    const projects = [project({ id: 3 }), project({ id: 1 }), project({ id: 2 })];
    const { columns } = buildBoard(projects, { react: [], drafts: [], runs: [], sent: [] }, NOW);
    expect(columns.map((c) => c.project.id)).toEqual([3, 1, 2]);
    expect(columns.map((c) => c.state.wait)).toEqual([false, false, false]);
  });

  it('waitCount = черновики проекта; нет рабочей папки — строки нет', () => {
    const home = { react: [], drafts: [outbox(1, 1), outbox(2, 2), outbox(3, 1)], runs: [], sent: [] };
    const projects = [project({ id: 1 }), project({ id: 2 }), project({ id: 3, workdir: null })];
    const { columns } = buildBoard(projects, home, NOW);
    expect(columns.map((c) => c.waitCount)).toEqual([2, 1, 0]);
    expect(columns[0]!.state).toEqual({ line: 'свободна', wait: false });
    expect(columns[2]!.state).toEqual({ line: '', wait: false });
  });
});

// --- монтирование ---------------------------------------------------------------------------------------------

interface Fixtures {
  react?: HomeReactItem[];
  drafts?: OutboxListItem[];
  runs?: AgentRun[];
  sent?: OutboxListItem[];
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function boardFetch(fx: Fixtures = {}) {
  return vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const p = url.pathname;
    if (p === '/api/v1/home/react') return jsonResponse(fx.react ?? []);
    if (p === '/api/v1/agent/runs') return jsonResponse(fx.runs ?? []);
    if (p === '/api/v1/outbox') {
      const status = url.searchParams.get('status');
      return jsonResponse(status === 'sent' ? (fx.sent ?? []) : status === 'queued' ? [] : (fx.drafts ?? []));
    }
    if (p.startsWith('/api/v1/projects/') && p.endsWith('/channels')) return jsonResponse([]);
    return jsonResponse({ status: 'ok' });
  });
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.home = { react: [], drafts: [], runs: [], sent: [], loadedAt: null, error: null };
  store.state.projects = [project({ id: 1 }), project({ id: 2, name: 'Initech', workdir: null })];
  store.state.sync.channelsById = {};
  store.state.homeOpen = true;
  store.state.homeAnchor = null;
  store.state.currentProjectId = null;
  store.state.currentChannelId = null;
  window.localStorage.clear();
});

const originalScrollIntoView = Element.prototype.scrollIntoView;

/** jsdom не реализует `scrollIntoView` — подменяем на время теста (снимается в `afterEach`). */
function stubScrollIntoView(impl: (this: Element, arg?: unknown) => void) {
  const spy = vi.fn(impl);
  Element.prototype.scrollIntoView = spy as unknown as Element['scrollIntoView'];
  return spy;
}

afterEach(() => {
  Element.prototype.scrollIntoView = originalScrollIntoView;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Ф10-6 A2: столбцы доски', () => {
  it('столбец на проект: шапка, «Ничего не ждёт» и серая строка про Claude; без чата оркестратора и «Новой задачи»; без рабочей папки — без строки', async () => {
    vi.stubGlobal('fetch', boardFetch());
    const wrapper = mount(HomeView);
    await flushPromises();
    const columns = wrapper.findAll('.board > .board-column');
    expect(columns.map((c) => c.attributes('data-project-id'))).toEqual(['1', '2']);
    const first = columns[0]!;
    expect(first.find('header.board-column-head').exists()).toBe(true);
    // Ф20-4: пустые секции не показываются — остаётся «Ничего не ждёт» и серая строка про Claude
    expect(first.findAll('[data-section]').map((s) => s.attributes('data-section'))).toEqual(['claude']);
    expect(first.get('.board-nothing').text()).toBe('Ничего не ждёт');
    expect(first.get('[data-section="claude"]').text()).toMatch(/^Claude сегодня: 0 · сессия: свободна ·\s+Агенты ›$/);
    expect(first.findAll('.board-section-title')).toHaveLength(0);
        // Initech без рабочей папки — ни строки про Claude, ни строки состояния
    expect(columns[1]!.find('[data-section="claude"]').exists()).toBe(false);
    expect(columns[1]!.find('.board-state').exists()).toBe(false);
    wrapper.unmount();
  });

  it('разделы с содержимым — заголовок со счётчиком; клик по имени проекта — openProject', async () => {
    vi.stubGlobal(
      'fetch',
      boardFetch({ react: [react(2, 21, { draft_count: 1 })], drafts: [outbox(8, 2), outbox(9, 2)] }),
    );
    const open = vi.spyOn(store, 'openProject').mockResolvedValue(undefined);
    const wrapper = mount(HomeView);
    await flushPromises();
    const col = wrapper.get('.board-column[data-project-id="2"]');
    expect(col.get('[data-section="react"] .board-section-title').text()).toBe('Нужна реакция · 1');
    const row = col.get('.home-react-row');
    expect(row.get('.home-react-address').text()).toBe('беседа 21');
    expect(row.get('.home-react-last').text()).toBe('Пётр: Когда?');
    expect(row.get('.home-react-draft').text()).toBe('черновик');
    expect(col.get('[data-section="drafts"] .board-section-title').text()).toBe('Черновики · 2');
    // Ф20-4: черновики — строками (полная карточка — в окне по «›»)
    expect(col.findAll('[data-section="drafts"] .draft-row')).toHaveLength(2);
    expect(col.find('.draft-card').exists()).toBe(false);
    expect(col.get('.draft-row-who').text()).toBe('Покупатель'); // беседа черновика, проект столбца не повторяется
    await col.get('.board-column-name').trigger('click');
    expect(open).toHaveBeenCalledWith(2);
    wrapper.unmount();
  });

  it('«Неразобранное» появляется последним узким столбцом, когда там ждут, и исчезает, когда нет', async () => {
    vi.stubGlobal('fetch', boardFetch({ react: [react(-1, 91)] }));
    const open = vi.spyOn(store, 'openProject').mockResolvedValue(undefined);
    const wrapper = mount(HomeView);
    await flushPromises();
    const ids = () => wrapper.findAll('.board > .board-column').map((c) => c.attributes('data-project-id'));
    expect(ids()).toEqual(['1', '2', '-1']);
    const unsorted = wrapper.get('.board-column[data-project-id="-1"]');
    expect(unsorted.classes()).toContain('board-column--unsorted');
    expect(unsorted.get('.board-column-name').text()).toBe('Неразобранное');
    // Ф20-4: пустые разделы не показываются (у «Неразобранного» строки про Claude нет)
    expect(unsorted.findAll('[data-section]').map((s) => s.attributes('data-section'))).toEqual(['react']);
    expect((wrapper.get('.board').element as HTMLElement).style.gridTemplateColumns).toBe(
      'repeat(2, minmax(270px, 1fr)) minmax(270px, 1fr)',
    );
    await unsorted.get('.board-column-name').trigger('click');
    expect(open).toHaveBeenCalledWith(-1);
    store.state.home.react = [];
    await flushPromises();
    expect(ids()).toEqual(['1', '2']);
    expect((wrapper.get('.board').element as HTMLElement).style.gridTemplateColumns).toBe(
      'repeat(2, minmax(270px, 1fr))',
    );
    wrapper.unmount();
  });

  it('homeAnchor = drafts — прокрутка к «Черновикам» первого столбца, где они есть, и подсветка', async () => {
    vi.stubGlobal('fetch', boardFetch({ drafts: [outbox(8, 2)] }));
    const scrolled: string[] = [];
    const spy = stubScrollIntoView(function (this: Element) {
      const col = this.closest('.board-column')?.getAttribute('data-project-id');
      scrolled.push(`${col}:${this.getAttribute('data-section')}`);
    });
    store.state.homeAnchor = 'drafts';
    const wrapper = mount(HomeView, { attachTo: document.body });
    await flushPromises();
    expect(scrolled).toEqual(['2:drafts']);
    expect(spy.mock.calls[0]![0]).toEqual({ block: 'start', inline: 'nearest' });
    expect(wrapper.get('.board-column[data-project-id="2"] [data-section="drafts"]').classes()).toContain('flash');
    expect(store.state.homeAnchor).toBeNull();
    // нет содержимого ни в одном столбце — к доске
    store.state.homeAnchor = 'sent';
    await flushPromises();
    expect(scrolled[1]).toBe('undefined:null');
    wrapper.unmount();
  });
});

// --- A4: телефон ------------------------------------------------------------------------------------------------

describe('Ф10-6 A4: телефонная доска', () => {
  it('без оркестратора; чипы проектов со счётчиком «ждёт»; тап прокручивает к столбцу и делает чип активным', async () => {
    vi.stubGlobal(
      'fetch',
      boardFetch({ react: [react(-1, 91)], drafts: [outbox(8, 2)] }),
    );
    const scrolled: (string | null)[] = [];
    stubScrollIntoView(function (this: Element) {
      scrolled.push(this.getAttribute('data-project-id'));
    });
    const wrapper = mount(HomeView, { props: { phone: true }, attachTo: document.body });
    await flushPromises();
    expect(wrapper.get('.board').classes()).toContain('board--phone');
    expect((wrapper.get('.board').element as HTMLElement).style.gridTemplateColumns).toBe('');
    const chips = wrapper.findAll('.board-chip');
    expect(chips.map((c) => c.attributes('data-project-id'))).toEqual(['1', '2', '-1']);
    expect(chips.map((c) => c.find('.badge-react').exists() ? c.get('.badge-react').text() : null)).toEqual([
      null,
      '1',
      null,
    ]);
    expect(chips[2]!.text()).toBe('Неразобранное');
    expect(chips.map((c) => c.attributes('aria-current') ?? null)).toEqual(['true', null, null]);
    await chips[1]!.trigger('click');
    expect(scrolled).toEqual(['2']);
    expect(wrapper.findAll('.board-chip').map((c) => c.attributes('aria-current') ?? null)).toEqual([
      null,
      'true',
      null,
    ]);
    wrapper.unmount();
  });
});
