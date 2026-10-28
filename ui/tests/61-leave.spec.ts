// Ф17-2b (`docs/architecture.md`: выход из групп и каналов Telegram — кнопка в
// шапке беседы и её состояния, подтверждение, карточка просьбы сессии, «Ждёт», строка в архивной беседе, загрузка
// заявок при открытии беседы (П12), применение `/sync.leaves` и кадра SSE `leave`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, LeaveRequest, Project } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import Composer from '../src/components/Composer.vue';
import MessageFeed from '../src/components/MessageFeed.vue';
import LeaveCard from '../src/components/LeaveCard.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createLeaveState } from '../src/store/leaves';
import { createStaleness } from '../src/store/staleness';
import { connectStream, type EventSourceLike } from '../src/store/stream';
import { createStore } from '../src/store/store';
import { buildBoard } from '../src/util/board';
import { canLeaveChannel, leaveButtonState, leaveConfirmText, leaveFailReason, visibleLeaves } from '../src/util/leaves';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 8, 30, 15, 0, 0).getTime();
const DAY = 24 * 60 * 60 * 1000;
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function leave(overrides: Partial<LeaveRequest> = {}): LeaveRequest {
  return {
    id: 1,
    account_id: 1,
    project_id: 3,
    channel_id: 7,
    title: 'Чат продавцов',
    chat_kind: 'group',
    reason: 'группа не по теме',
    origin: 'session',
    status: 'pending',
    result: null,
    error: null,
    client_name: 'маркетолог',
    created_ts: NOW - 60_000,
    decided_ts: null,
    done_ts: null,
    returned_ts: null,
    updated_ts: NOW - 60_000,
    can_decline: true,
    leaving: false,
    seq: 10,
    ...overrides,
  };
}

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: 7,
    ext_id: '-100777',
    kind: 'group',
    title: 'Чат продавцов',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 1000,
    unread: 0,
    project_id: 3,
    source: 'telegram',
    ...overrides,
  };
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, ...overrides };
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

function fakeFetch(handler: (s: Sent) => Response | undefined = () => undefined): {
  sent: Sent[];
  fetchMock: ReturnType<typeof vi.fn>;
} {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: url.pathname,
      query: url.search,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    sent.push(s);
    const custom = handler(s);
    if (custom !== undefined) return custom;
    if (s.path === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (s.path === '/api/v1/status') return jsonResponse({ accounts: [] });
    if (s.path.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
    if (s.method === 'GET') return jsonResponse([]);
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.leaves = createLeaveState();
  store.state.projects = [project()];
  store.state.homeOpen = true;
  store.state.sync.channelsById = {};
  store.state.currentProjectId = 3;
  store.state.currentChannelId = null;
  delete (window as unknown as { __xss?: number }).__xss;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('Ф17-2b: util/leaves', () => {
  it('кнопка — только беседы Telegram group|channel|topic не в архиве', () => {
    expect(canLeaveChannel(channel())).toBe(true);
    expect(canLeaveChannel(channel({ kind: 'channel' }))).toBe(true);
    expect(canLeaveChannel(channel({ kind: 'topic' }))).toBe(true);
    expect(canLeaveChannel(channel({ kind: 'dm' }))).toBe(false);
    expect(canLeaveChannel(channel({ kind: 'mail_thread' }))).toBe(false);
    expect(canLeaveChannel(channel({ source: 'slack' }))).toBe(false);
    expect(canLeaveChannel(channel({ source: undefined }))).toBe(false);
    expect(canLeaveChannel(channel({ archived: true }))).toBe(false);
    expect(canLeaveChannel(null)).toBe(false);
  });

  it('причины простыми словами, прочее — как есть', () => {
    expect(leaveFailReason('creator')).toBe('вы создатель, Telegram не выпускает создателя канала');
    expect(leaveFailReason('not_found')).toBe('беседы уже нет в Telegram');
    expect(leaveFailReason('forbidden')).toBe('Telegram не дал выйти');
    expect(leaveFailReason('gave_up')).toBe('Telegram не ответил, попробуйте ещё раз');
    expect(leaveFailReason('FloodWaitError')).toBe('FloodWaitError');
    expect(leaveFailReason(null)).toBe('не получилось');
    expect(leaveConfirmText('Чат')).toBe('Выйти из «Чат» в Telegram? Беседа уйдёт в архив хаба, история сохранится');
  });

  it('карточкой видны только ждущие и одобренные', () => {
    const list = [
      leave({ id: 2, status: 'approved' }),
      leave({ id: 1, status: 'pending' }),
      leave({ id: 3, status: 'failed' }),
      leave({ id: 4, status: 'left' }),
      leave({ id: 5, status: 'declined' }),
    ];
    expect(visibleLeaves(list).map((l) => l.id)).toEqual([1, 2]);
  });

  it('состояние кнопки решает самая новая заявка; failed — 7 дней', () => {
    expect(leaveButtonState([], NOW)).toEqual({ leaving: false, failure: null, left: false });
    expect(leaveButtonState([leave({ status: 'approved' })], NOW).leaving).toBe(true);
    expect(leaveButtonState([leave({ status: 'pending' })], NOW)).toEqual({ leaving: false, failure: null, left: false });
    const failed = leave({ id: 2, status: 'failed', error: 'creator', updated_ts: NOW - DAY });
    expect(leaveButtonState([failed], NOW).failure).toBe('вы создатель, Telegram не выпускает создателя канала');
    expect(leaveButtonState([{ ...failed, updated_ts: NOW - 8 * DAY }], NOW).failure).toBeNull();
    // новая открытая заявка важнее старой неудачи
    expect(leaveButtonState([failed, leave({ id: 3, status: 'approved' })], NOW)).toMatchObject({ leaving: true, failure: null });
    expect(leaveButtonState([leave({ status: 'left' })], NOW).left).toBe(true);
    // Хаб вернул беседу (ревью 17-2a): выход не в силе — поздний архив владельца не «Вы вышли».
    expect(leaveButtonState([leave({ status: 'left', returned_ts: NOW - DAY })], NOW).left).toBe(false);
  });
});

describe('Ф17-2b: кнопка в шапке беседы', () => {
  async function mountView(channelOverrides: Partial<Channel> = {}) {
    store.state.sync.channelsById[7] = channel(channelOverrides);
    const wrapper = mount(ChannelView, { props: { channelId: 7 }, attachTo: document.body });
    await flushPromises();
    return wrapper;
  }

  it('подпись по виду: группа, канал, форум; у личек, Slack и архивных кнопки нет', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    for (const [kind, text] of [
      ['group', 'Выйти из группы'],
      ['channel', 'Выйти из канала'],
      ['topic', 'Выйти из форума'],
    ] as const) {
      const w = await mountView({ kind });
      expect(w.get('button.leave-channel-button').text()).toBe(text);
      w.unmount();
    }
    for (const over of [{ kind: 'dm' as const }, { source: 'slack' }, { archived: true }]) {
      const w = await mountView(over);
      expect(w.find('button.leave-channel-button').exists()).toBe(false);
      w.unmount();
    }
  });

  it('подтверждение: отмена — ничего, «ОК» — POST /channels/7/leave', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/channels/7/leave' ? jsonResponse(leave({ status: 'approved', origin: 'ui', can_decline: false, leaving: true, seq: 11 })) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal('confirm', confirm);
    const wrapper = await mountView();
    const button = wrapper.get('button.leave-channel-button');
    await button.trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenCalledWith('Выйти из «Чат продавцов» в Telegram? Беседа уйдёт в архив хаба, история сохранится');
    expect(sent.some((s) => s.method === 'POST')).toBe(false);
    await button.trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.method === 'POST').map((s) => s.path)).toContain('/api/v1/channels/7/leave');
    // заявка легла в стор — кнопка «Выхожу…» неактивна
    expect(wrapper.get('button.leave-channel-button').text()).toBe('Выхожу…');
    expect(wrapper.get('button.leave-channel-button').attributes('disabled')).toBeDefined();
    wrapper.unmount();
  });

  it('открытая approved — «Выхожу…», неактивна; pending от сессии — обычная активная кнопка', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    store.state.leaves.items[1] = leave({ status: 'approved', leaving: true });
    const leaving = await mountView();
    expect(leaving.get('button.leave-channel-button').text()).toBe('Выхожу…');
    expect(leaving.get('button.leave-channel-button').attributes('disabled')).toBeDefined();
    leaving.unmount();
    store.state.leaves.items[1] = leave({ status: 'pending', seq: 12 });
    const pending = await mountView();
    expect(pending.get('button.leave-channel-button').text()).toBe('Выйти из группы');
    expect(pending.get('button.leave-channel-button').attributes('disabled')).toBeUndefined();
    pending.unmount();
  });

  it('failed за 7 дней — кнопка активна, рядом серым «не вышло: причина»', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    store.state.leaves.items[1] = leave({ status: 'failed', error: 'creator', updated_ts: Date.now() - DAY });
    const wrapper = await mountView();
    expect(wrapper.get('button.leave-channel-button').attributes('disabled')).toBeUndefined();
    expect(wrapper.get('.leave-failure').text()).toBe('не вышло: вы создатель, Telegram не выпускает создателя канала');
    wrapper.unmount();
  });

  it('ошибка ручки — строкой рядом с шапкой', async () => {
    const { fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/channels/7/leave' ? jsonResponse({ detail: 'по этой беседе идёт вступление (заявка #4)' }, 409) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    const wrapper = await mountView();
    await wrapper.get('button.leave-channel-button').trigger('click');
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain('идёт вступление');
    wrapper.unmount();
  });

  it('при открытии беседы Telegram зовёт GET /leaves?channel_id=; у личек — нет (П12)', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/leaves' ? jsonResponse([leave({ status: 'approved', leaving: true })]) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = await mountView();
    const call = sent.find((s) => s.path === '/api/v1/leaves');
    expect(call?.query).toContain('channel_id=7');
    expect(store.state.leaves.items[1]).toBeDefined();
    expect(wrapper.get('button.leave-channel-button').text()).toBe('Выхожу…');
    wrapper.unmount();
    sent.length = 0;
    const dm = await mountView({ kind: 'dm' });
    expect(sent.some((s) => s.path === '/api/v1/leaves')).toBe(false);
    dm.unmount();
  });

  it('тема форума: заявки форума (другой channel_id) из ответа считаются заявками беседы', async () => {
    const { fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/leaves' ? jsonResponse([leave({ channel_id: 99, status: 'approved', leaving: true })]) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = await mountView({ kind: 'topic' });
    expect(wrapper.get('button.leave-channel-button').text()).toBe('Выхожу…');
    wrapper.unmount();
  });

  it('архивная беседа после выхода: лента остаётся, вместо поля ввода строка «Вы вышли…»', async () => {
    const { fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/leaves' ? jsonResponse([leave({ status: 'left', result: 'left' })]) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = await mountView({ archived: true });
    expect(wrapper.get('.leave-note').text()).toBe('Вы вышли, беседа в архиве хаба');
    expect(wrapper.findComponent(Composer).exists()).toBe(false);
    expect(wrapper.findComponent(MessageFeed).exists()).toBe(true);
    wrapper.unmount();
  });

  it('архив владельца после возврата (returned_ts) — не «Вы вышли», поле ввода как было', async () => {
    const { fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/leaves'
        ? jsonResponse([leave({ status: 'left', result: 'left', done_ts: NOW - DAY, returned_ts: NOW - 1000 })])
        : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = await mountView({ archived: true });
    expect(wrapper.find('.leave-note').exists()).toBe(false);
    expect(wrapper.findComponent(Composer).exists()).toBe(true);
    wrapper.unmount();
  });

  it('архивная беседа без заявки выхода (архив Ф11-9c) — поле ввода как было, строки нет', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = await mountView({ archived: true });
    expect(wrapper.find('.leave-note').exists()).toBe(false);
    expect(wrapper.findComponent(Composer).exists()).toBe(true);
    wrapper.unmount();
  });
});

describe('Ф17-2b: карточка', () => {
  it('название, вид, «Зачем» и от кого; «Выйти» и «Оставить» зовут approve / decline', async () => {
    const { sent, fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(LeaveCard, { props: { leave: leave() }, attachTo: document.body });
    const text = wrapper.text();
    expect(text).toContain('Чат продавцов');
    expect(text).toContain('группа');
    expect(text).toContain('Зачем: группа не по теме · маркетолог');
    await wrapper.get('button.leave-approve').trigger('click');
    await flushPromises();
    await wrapper.get('button.leave-decline').trigger('click');
    await flushPromises();
    const posts = sent.filter((s) => s.method === 'POST').map((s) => s.path);
    expect(posts).toContain('/api/v1/leaves/1/approve');
    expect(posts).toContain('/api/v1/leaves/1/decline');
    wrapper.unmount();
  });

  it('клик по названию открывает беседу', async () => {
    const open = vi.spyOn(store, 'openChannelAt').mockResolvedValue();
    const wrapper = mount(LeaveCard, { props: { leave: leave({ chat_kind: 'channel' }) } });
    expect(wrapper.text()).toContain('канал');
    await wrapper.get('button.leave-title').trigger('click');
    expect(open).toHaveBeenCalledWith(3, 7);
  });

  it('«выхожу…» без «Выйти»; «Оставить» — только пока can_decline', () => {
    const early = mount(LeaveCard, { props: { leave: leave({ status: 'approved', can_decline: true }) } });
    expect(early.text()).toContain('выхожу…');
    expect(early.find('button.leave-approve').exists()).toBe(false);
    expect(early.find('button.leave-decline').exists()).toBe(true);
    const leaving = mount(LeaveCard, { props: { leave: leave({ status: 'approved', can_decline: false, leaving: true }) } });
    expect(leaving.find('button.leave-decline').exists()).toBe(false);
    const noDecline = mount(LeaveCard, { props: { leave: leave({ can_decline: false }) } });
    expect(noDecline.find('button.leave-approve').exists()).toBe(true);
    expect(noDecline.find('button.leave-decline').exists()).toBe(false);
  });

  it('409 при решении — заявки перечитываются, показана строка', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/leaves/1/approve' ? jsonResponse({ detail: 'уже' }, 409) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(LeaveCard, { props: { leave: leave() } });
    await wrapper.get('button.leave-approve').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/leaves')).toBe(true);
    expect(wrapper.get('.leave-error').text()).toContain('уже изменилась');
  });

  it('название и причина — только текстом, не HTML', () => {
    const wrapper = mount(LeaveCard, {
      props: { leave: leave({ title: XSS, reason: XSS, client_name: XSS }) },
      attachTo: document.body,
    });
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.find('script').exists()).toBe(false);
    expect(wrapper.text()).toContain('<img src=x');
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
    wrapper.unmount();
  });
});

describe('Ф17-2b: стор — /sync и кадр SSE', () => {
  it('/sync.leaves попадает в стор; старшая по seq побеждает', async () => {
    let calls = 0;
    const { fetchMock } = fakeFetch((s) => {
      if (s.path !== '/api/v1/sync') return undefined;
      calls += 1;
      const leaves = calls === 1 ? [leave({ seq: 11 })] : [leave({ seq: 9, status: 'declined' }), leave({ id: 2, seq: 12 })];
      return jsonResponse({ messages: [], channels: [], max_seq: 10 + calls, more: false, leaves });
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.runSync();
    expect(s.state.leaves.items[1]!.status).toBe('pending');
    await s.runSync();
    expect(s.state.leaves.items[1]!.status).toBe('pending');
    expect(s.state.leaves.items[2]).toBeDefined();
  });

  it('кадр SSE leave запускает /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      readyState: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      close: () => {},
    };
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('leave')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
  });

  it('loadLeaves читает GET /leaves (с channel_id — заявки беседы)', async () => {
    const { sent, fetchMock } = fakeFetch((s) => (s.path === '/api/v1/leaves' ? jsonResponse([leave()]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    expect(await s.loadLeaves()).toEqual([7]);
    expect(sent[0]!.query).not.toContain('channel_id');
    await s.loadLeaves(7);
    expect(sent[1]!.query).toContain('channel_id=7');
    expect(s.state.leaves.items[1]!.id).toBe(1);
  });
});

describe('Ф17-2b: главная и «Ждёт»', () => {
  const HOME = { react: [], drafts: [], runs: [], sent: [] };
  const WORK = { tasks: {}, pending: {}, stale: {} };

  it('доска: просьба — в столбце своего проекта, ждущая входит в счётчик; без проекта — столбца нет', () => {
    const board = buildBoard(
      [project()],
      { ...HOME, leaves: [leave({ id: 1 }), leave({ id: 2, status: 'left' }), leave({ id: 3, project_id: null, origin: 'ui' })] },
      NOW,
    );
    expect(board.columns[0]!.leaves.map((l) => l.id)).toEqual([1]);
    expect(board.columns[0]!.waitCount).toBe(1);
  });

  it('заявка «Выйти?» — карточкой в «Нужна реакция» столбца проекта (Ф20-4: отдельной секции нет)', () => {
    const board = buildBoard([project()], { ...HOME, leaves: [leave()] }, NOW);
    const wrapper = mount(ProjectColumn, { props: { column: board.columns[0]! } });
    expect(wrapper.get('[data-section="react"] .board-section-title').text()).toBe('Нужна реакция · 1');
    expect(wrapper.find('[data-section="react"] [data-section="leaves"]').exists()).toBe(true);
    expect(wrapper.find('button.leave-approve').exists()).toBe(true);
  });

  it('«Ждёт»: элемент leave; счётчик считает только ждущие решения', () => {
    const input = {
      projects: [project()],
      home: HOME,
      
      nowMs: NOW,
      leaves: [leave({ id: 1 }), leave({ id: 2, status: 'approved', can_decline: false })],
    };
    expect(buildWaiting(input).filter((i) => i.kind === 'leave')).toHaveLength(2);
    expect(homeDecisionCount(input)).toBe(1);
  });

  it('WaitList рисует карточку просьбы', async () => {
    const { fetchMock } = fakeFetch((s) => (s.path === '/api/v1/leaves' ? jsonResponse([leave()]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    store.state.leaves.items[1] = leave();
    const wrapper = mount(WaitList);
    await flushPromises();
    expect(wrapper.find('.wait-item--leave .leave-card').exists()).toBe(true);
    wrapper.unmount();
  });
});
