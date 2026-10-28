// Ф41-6б (`docs/architecture.md`): столбец беседы на «Главной» (прочитанность только у
// активного столбца, присутствие, `clearFocus` по id, экран и текущая беседа не меняются, тред — вкладкой проекта),
// линии «не в сети», «Забрать в хаб», сумма расхода за день.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, HubSession, Message, OfflineLine, Project } from '../src/api/types';
import BoardChannel from '../src/components/BoardChannel.vue';
import BoardColumns from '../src/components/BoardColumns.vue';
import BoardList from '../src/components/BoardList.vue';
import BoardToolbar from '../src/components/BoardToolbar.vue';
import { boardState, loadOfflineLines, openColumn, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

let sent: Sent[] = [];
let offlineAnswer: () => Response = () => json({ detail: 'not found' }, 404);
let sessionsToday: unknown;

function stubFetch(): void {
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      sent.push({ method, path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const p = url.pathname;
      if (p === '/api/v1/subs' && url.searchParams.get('offline') === '1') return offlineAnswer();
      if (p === '/api/v1/subs') return json({ items: [], channels: [] });
      if (p === '/api/v1/sessions') return json({ items: [], today: sessionsToday });
      if (/^\/api\/v1\/sessions\/\d+$/.test(p)) return json({ session: session({ id: Number(p.split('/').pop()) }), requests: [], says: [] });
      if (/\/events$/.test(p)) return json([]);
      if (/\/channels\/\d+\/messages$/.test(p) || /\/threads\/.+\/messages$/.test(p)) return json({ messages: [], next_before: null });
      if (p === '/api/v1/outbox') return json([]);
      return json([]);
    }),
  );
}

class StubObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true, ...overrides };
}

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: 283,
    source: 'telegram',
    kind: 'private',
    title: 'Покупатель',
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: NOW,
    my_msg_count: 0,
    first_seen_ts: NOW,
    unread: 2,
    project_id: 3,
    ...overrides,
  } as Channel;
}

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 283,
    project_id: 3,
    ext_id: '1',
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'текст',
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
    sender: null,
    ...overrides,
  };
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 9,
    project_id: 3,
    role: 'маркетолог',
    role_no: 1,
    engine: 'claude',
    model: null,
    effort: null,
    mode: 'manual',
    title: null,
    state: 'asleep',
    host_seen_ts: NOW,
    created_ts: NOW,
    updated_ts: NOW,
    closed_ts: null,
    close_reason: null,
    created_by: 'adopt',
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

function offline(overrides: Partial<OfflineLine> = {}): OfflineLine {
  return {
    key: 'p3:исследователь:1',
    project_id: 3,
    role: 'исследователь',
    role_no: 1,
    engine: 'claude',
    session_name: null,
    cwd: '/p',
    ended_ts: NOW - 60_000,
    end_reason: 'gone',
    pending: 0,
    sub_id: 77,
    ...overrides,
  };
}


beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  offlineAnswer = () => json({ detail: 'not found' }, 404);
  sessionsToday = undefined;
  stubFetch();
  vi.stubGlobal('IntersectionObserver', StubObserver);
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  vi.spyOn(document, 'hasFocus').mockReturnValue(true);
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project()];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
  store.state.sync.channelsById[283] = channel();
  store.state.sync.messagesByChannel[283] = [msg({ id: 11, ext_id: '11' }), msg({ id: 12, ext_id: '12', ts: 1001 })];
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  store.state.currentChannelId = null;
  store.state.openThread = null;
  store.state.focus = null;
  store.state.boardActiveChannelId = null;
});

afterEach(() => {
  resetBoard();
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  store.state.boardActiveChannelId = null;
  delete store.state.sync.messagesByChannel[283];
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-6б: столбец беседы — прочитанность', () => {
  it('без указателя и фокуса «прочитано» не уходит; навёл указатель — уходит', async () => {
    vi.useFakeTimers();
    const markRead = vi.spyOn(store, 'markRead').mockResolvedValue();
    const w = mount(BoardChannel, { props: { item: { kind: 'channel', id: 283 }, channelId: 283 }, attachTo: document.body });
    await vi.advanceTimersByTimeAsync(0);
    window.dispatchEvent(new Event('focus')); // возврат фокуса окна перезапускает отправку — но беседа не активна
    await vi.advanceTimersByTimeAsync(1500);
    expect(markRead).not.toHaveBeenCalled();
    await w.get('.board-col').trigger('pointerenter');
    await vi.advanceTimersByTimeAsync(1500);
    expect(markRead).toHaveBeenCalledWith(283, 12);
    w.unmount();
  });

  it('фокус внутри столбца делает его активным; уход фокуса наружу — нет', async () => {
    vi.useFakeTimers();
    const markRead = vi.spyOn(store, 'markRead').mockResolvedValue();
    const w = mount(BoardChannel, { props: { item: { kind: 'channel', id: 283 }, channelId: 283 }, attachTo: document.body });
    await vi.advanceTimersByTimeAsync(0);
    await w.get('.board-col').trigger('focusin');
    await vi.advanceTimersByTimeAsync(1500);
    expect(markRead).toHaveBeenCalledTimes(1);
    expect(w.get('.board-channel').attributes('data-active')).toBe('1');
    await w.get('.board-col').trigger('focusout');
    expect(w.get('.board-channel').attributes('data-active')).toBe('0');
    w.unmount();
  });

  it('в шапке — «N новых» и метка источника', () => {
    const w = mount(BoardChannel, { props: { item: { kind: 'channel', id: 283 }, channelId: 283 } });
    expect(w.get('.board-channel-unread').text()).toBe('2 новых');
    expect(w.get('.board-channel-source').text()).toBe('TG');
    expect(w.find('.board-col-project').exists()).toBe(false); // Ф90-3: проект — в «⋯»
    expect(w.find('.board-channel-foot').exists()).toBe(false);
    expect(w.get('.board-col-name').text()).toBe('Покупатель');
    w.unmount();
  });
});

describe('Ф41-6б: столбец беседы не меняет экран', () => {
  it('currentChannelId, homeOpen, openThread не меняются; страница грузится `loadChannelPage`', async () => {
    const w = mount(BoardChannel, { props: { item: { kind: 'channel', id: 283 }, channelId: 283 } });
    await flushPromises();
    expect(sent.some((s) => s.path === '/api/v1/channels/283/messages')).toBe(true);
    expect(store.state.currentChannelId).toBeNull();
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.openThread).toBeNull();
    w.unmount();
  });

  it('клик «беседа» из списка слева открывает столбец с целью прокрутки, экран остаётся', async () => {
    await store.openChannelAt(3, 283, { messageId: 12 }, { column: true });
    await flushPromises();
    expect(boardState.layout.open).toEqual([{ kind: 'channel', id: 283 }]);
    expect(store.state.focus).toMatchObject({ channelId: 283, messageId: 12 });
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.currentChannelId).toBeNull();
  });

  it('вне «Главной» (вкладка проекта) тот же клик открывает беседу обычным видом', async () => {
    store.state.homeOpen = false;
    store.state.currentProjectId = 3;
    await store.openChannelAt(3, 283);
    await flushPromises();
    expect(store.state.currentChannelId).toBe(283);
    expect(boardState.layout.open).toEqual([]);
  });

  it('тред из столбца ведёт на вкладку проекта с этим тредом', async () => {
    await store.openThreadInProject(283, 'P1');
    await flushPromises();
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.currentChannelId).toBe(283);
    expect(store.state.openThread).toEqual({ channelId: 283, threadExtId: 'P1' });
  });

  it('«все беседы проекта» открывает беседу вкладкой проекта', async () => {
    const w = mount(BoardChannel, { props: { item: { kind: 'channel', id: 283 }, channelId: 283 } });
    await flushPromises();
    await w.get('.board-col-menu').trigger('click');
    await w.get('.board-channel-all').trigger('click');
    await flushPromises();
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.currentChannelId).toBe(283);
    w.unmount();
  });
});

describe('Ф41-6б: цель прокрутки и присутствие', () => {
  it('clearFocus(id) чужой беседы цель не снимает; своей — снимает; без аргумента — снимает', () => {
    store.state.focus = { channelId: 1, messageId: 5, outboxId: null, seq: 1 };
    store.clearFocus(2);
    expect(store.state.focus).not.toBeNull();
    store.clearFocus(1);
    expect(store.state.focus).toBeNull();
    store.state.focus = { channelId: 1, messageId: 5, outboxId: null, seq: 2 };
    store.clearFocus();
    expect(store.state.focus).toBeNull();
  });

  it('присутствие на «Главной» — беседа активного столбца, без активного — null', async () => {
    const target = (): unknown => (store as unknown as { presenceTarget(): unknown }).presenceTarget();
    const w = mount(BoardChannel, { props: { item: { kind: 'channel', id: 283 }, channelId: 283 } });
    await flushPromises();
    expect(target()).toBeNull();
    await w.get('.board-col').trigger('pointerenter');
    expect(target()).toEqual({ channelId: 283, threadExtId: null });
    await w.get('.board-col').trigger('pointerleave');
    expect(target()).toBeNull();
    await w.get('.board-col').trigger('pointerenter');
    w.unmount(); // размонтирование снимает активность
    expect(target()).toBeNull();
  });
});

describe('Ф41-6б: линии «не в сети» и «Забрать в хаб»', () => {
  it('ручки нет (404) — линий нет, ошибки нет', async () => {
    await loadOfflineLines();
    expect(boardState.offline).toEqual([]);
  });

  it('линия «не в сети» — серая строка в списке проекта; клик открывает столбец', async () => {
    offlineAnswer = () => json({ lines: [offline()] });
    await loadOfflineLines();
    const w = mount(BoardList);
    const row = w.get('[data-agent-key="p3:исследователь:1"]');
    expect(row.text()).toContain('исследователь');
    expect(row.text()).toContain('не в сети');
    expect(row.get('.board-dot').classes()).toContain('board-dot--idle');
    await row.trigger('click');
    expect(boardState.layout.open).toEqual([{ kind: 'agent', key: 'p3:исследователь:1' }]);
    w.unmount();
  });
});

describe('Ф41-6б: сумма расхода за день', () => {
  it('поля `today` нет — без суммы; пришло — «$X сегодня»', async () => {
    let w = mount(BoardToolbar);
    expect(w.get('.board-toolbar-summary').text()).not.toContain('$');
    w.unmount();
    sessionsToday = { cost_usd: 12.5, turns: 30 };
    await store.loadSessions();
    w = mount(BoardToolbar);
    expect(w.get('.board-toolbar-summary').text()).toContain('$12.50 сегодня');
    w.unmount();
  });
});

function resetBoardOpen(): void {
  boardState.layout.open = [];
}

