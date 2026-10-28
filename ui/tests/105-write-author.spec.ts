// Ф46-1b: «Написать автору» — личка автору сообщения группы Telegram (`docs/architecture.md`, П3, П9).
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, Message } from '../src/api/types';
import ChannelModal from '../src/components/ChannelModal.vue';
import ChannelView from '../src/components/ChannelView.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { newRecipientWho } from '../src/util/home';
import { layout } from '../src/util/layout';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

class StubObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

function channel(over: Partial<Channel> = {}): Channel {
  return {
    id: 536, ext_id: '-100', source: 'telegram', kind: 'group', title: 'Ozon владельцы', tier: null, muted: false,
    archived: false, map_reason: null, last_msg_ts: 1, my_msg_count: 0, first_seen_ts: 1, unread: 0, project_id: 3,
    ...over,
  } as Channel;
}

function msg(over: Partial<Message> = {}): Message {
  return {
    id: 55, seq: 1, first_seq: 1, channel_id: 536, project_id: 3, ext_id: '55886', thread_ext_id: null,
    thread_broadcast: false, purged: false, thread: null, reply_to_ext_id: null, grouped_id: null, is_me: false,
    kind: 'text', text: 'привет', entities_json: null, attachments_json: null, forward_json: null,
    mentions_me: false, refs_json: null, reactions_json: null, ts: 1000, edited_ts: null, deleted: false,
    outbox_id: null, outbox_status: null, sender: { id: 1, name: 'Иван', handle: null }, ...over,
  };
}

const DRAFT = channel({
  id: 900, kind: 'dm', title: 'Иван',
  new_to: { kind: 'tg_msg_author', name: 'Иван', from_title: 'Ozon владельцы', from_channel_id: 536, from_message_id: 55 },
});

let calls: { path: string; method: string; body: unknown }[];
let createResponse: () => Response;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  calls = [];
  createResponse = () => json({ channel: DRAFT, created: true });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const p = new URL(input, 'http://localhost').pathname;
    const method = init?.method ?? 'GET';
    calls.push({ path: p, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    if (p.endsWith('/channels/new')) return createResponse();
    if (/\/channels\/536$/.test(p)) return json({ account_id: 1 });
    if (/\/messages$/.test(p)) return json({ messages: [], next_before: null });
    return json([]);
  }));
  vi.stubGlobal('IntersectionObserver', StubObserver);
  store.state.sync.channelsById = { 536: channel() };
  store.state.sync.messagesByChannel = { 536: [], 900: [] };
  store.state.homeOpen = true;
  store.state.channelModalId = null;
  layout.mode = 'full';
});

afterEach(() => {
  resetBoard();
  layout.mode = 'full';
  store.state.channelModalId = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function button(over: Partial<Message> = {}, ch: Partial<Channel> = {}) {
  store.state.sync.channelsById[536] = channel(ch);
  return mount(MessageRow, { props: { message: msg(over) } }).find('.write-author-button');
}

describe('видимость «Написать автору»', () => {
  it('группа и тема Telegram — есть', () => {
    expect(button().exists()).toBe(true);
    expect(button({}, { kind: 'topic' }).exists()).toBe(true);
  });
  it.each([
    ['личка', {}, { kind: 'dm' as const }],
    ['канал', {}, { kind: 'channel' as const }],
    ['моё', { is_me: true }, {}],
    ['бот', { is_bot: true }, {}],
    ['без автора', { sender: null }, {}],
    ['не Telegram', {}, { source: 'slack' as const }],
  ])('%s — нет', (_n, m, c) => {
    expect(button(m as Partial<Message>, c as Partial<Channel>).exists()).toBe(false);
  });
});

describe('клик «Написать автору»', () => {
  it('шлёт /channels/new с аккаунтом, видом, id сообщения и проектом беседы; открывает окном', async () => {
    const w = mount(MessageRow, { props: { message: msg() } });
    await w.get('.write-author-button').trigger('click');
    await flushPromises();
    const call = calls.find((c) => c.path.endsWith('/channels/new'))!;
    expect(call.method).toBe('POST');
    expect(call.body).toEqual({
      account_id: 1,
      project_id: 3,
      to: { kind: 'tg_msg_author', message_id: 55 },
    });
    expect(store.state.channelModalId).toBe(900);
  });

  it('беседа в «Неразобранном» — project_id null', async () => {
    store.state.sync.channelsById[536] = channel({ project_id: null });
    const w = mount(MessageRow, { props: { message: msg() } });
    await w.get('.write-author-button').trigger('click');
    await flushPromises();
    const call = calls.find((c) => c.path.endsWith('/channels/new'))!;
    expect((call.body as { project_id: unknown }).project_id).toBeNull();
  });

  it('телефон — обычный переход к беседе, окна нет', async () => {
    layout.mode = 'phone';
    const w = mount(MessageRow, { props: { message: msg() } });
    await w.get('.write-author-button').trigger('click');
    await flushPromises();
    expect(store.state.channelModalId).toBeNull();
    expect(store.state.currentChannelId).toBe(900);
  });

  it('из вкладки проекта — окно поверх, маршрут и открытая беседа не меняются; закрытие возвращает как было', async () => {
    store.state.homeOpen = false;
    store.state.currentProjectId = 3;
    store.state.currentChannelId = 536;
    const w = mount(MessageRow, { props: { message: msg() } });
    await w.get('.write-author-button').trigger('click');
    await flushPromises();
    expect(store.state.channelModalId).toBe(900);
    expect(store.state.channelModalOverlay).toBe(true);
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.currentProjectId).toBe(3);
    expect(store.state.currentChannelId).toBe(536);
    const modal = mount(ChannelModal, { props: { channelId: 900 }, attachTo: document.body });
    await flushPromises();
    expect(modal.find('[role="dialog"]').exists()).toBe(true);
    expect(modal.find('.channel-modal-board').exists()).toBe(false);
    await modal.get('.channel-modal-close').trigger('click');
    expect(store.state.channelModalId).toBeNull();
    expect(store.state.channelModalOverlay).toBe(false);
    expect(store.state.currentChannelId).toBe(536);
    expect(store.state.homeOpen).toBe(false);
    modal.unmount();
  });

  it('окно поверх не закрывается уходом с «Главной»', async () => {
    store.state.homeOpen = true;
    const w = mount(MessageRow, { props: { message: msg() } });
    await w.get('.write-author-button').trigger('click');
    await flushPromises();
    const modal = mount(ChannelModal, { props: { channelId: 900 }, attachTo: document.body });
    store.state.homeOpen = false;
    await flushPromises();
    expect(store.state.channelModalId).toBe(900);
    modal.unmount();
  });

  it('ошибка ядра показана текстом, окно не открыто', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    createResponse = () => json({ detail: 'автор — бот' }, 422);
    const w = mount(MessageRow, { props: { message: msg() } });
    await w.get('.write-author-button').trigger('click');
    await flushPromises();
    expect(w.get('.msg-op-error').text()).toContain('автор — бот');
    expect(store.state.channelModalId).toBeNull();
  });
});

describe('заготовка tg_msg_author', () => {
  it('newRecipientWho: имя и группа; без группы — имя', () => {
    expect(newRecipientWho(DRAFT.new_to)).toBe('Иван · из группы «Ozon владельцы»');
    expect(newRecipientWho({ kind: 'tg_msg_author', name: 'Иван' })).toBe('Иван');
  });

  it('шапка: «Новая личка · из группы «…»» и ссылка «к сообщению» открывает группу на сообщении', async () => {
    store.state.sync.channelsById[900] = DRAFT;
    const w = mount(ChannelView, { props: { channelId: 900 } });
    expect(w.get('.new-recipient-line').text()).toContain('Новая личка · из группы «Ozon владельцы»');
    const spy = vi.spyOn(store, 'openChannelAt').mockResolvedValue(undefined);
    await w.get('.author-source-link').trigger('click');
    expect(spy).toHaveBeenCalledWith(3, 536, { messageId: 55 });
  });

  it('нет from_* — ссылки нет', () => {
    store.state.sync.channelsById[900] = channel({ id: 900, kind: 'dm', new_to: { kind: 'tg_msg_author', name: 'Иван' } });
    const w = mount(ChannelView, { props: { channelId: 900 } });
    expect(w.find('.author-source-link').exists()).toBe(false);
    expect(w.get('.new-recipient-line').text()).toContain('Иван');
  });
});
