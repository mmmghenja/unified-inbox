// Ф8-4 (`docs/architecture.md`), тесты T8–T10: «Удалить у себя» с подтверждением (строка,
// альбом — П11, беседа), `hidden: true` из `/sync` убирает сообщение и беседу, «изменено» раскрывает версии.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, Message, SyncResponse } from '../src/api/types';
import AlbumRow from '../src/components/AlbumRow.vue';
import ChannelView from '../src/components/ChannelView.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { applySync, createSyncState } from '../src/store/applySync';
import { store } from '../src/store/instance';
import { createStore } from '../src/store/store';
import { threadKey } from '../src/store/threads';
import { HIDE_CHANNEL_CONFIRM, HIDE_MESSAGE_CONFIRM } from '../src/util/hide';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 1,
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
    sender: { id: 1, name: 'Коллега', handle: null },
    ...overrides,
  };
}

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 5,
    ext_id: 'c5',
    kind: 'dm',
    title: 'Беседа',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 0,
    unread: 0,
    project_id: 1,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  store.state.sync.threads = {};
  store.state.currentChannelId = null;
  store.state.openThread = null;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('T8: «Удалить у себя» — сообщение и альбом', () => {
  it('подтверждение — DELETE, отмена — ничего', async () => {
    const hide = vi.spyOn(store, 'hideMessages').mockResolvedValue();
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal('confirm', confirm);
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 42 }) } });
    const button = wrapper.get('button.hide-button');
    expect(button.text()).toBe('Удалить у себя');

    await button.trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenCalledWith(HIDE_MESSAGE_CONFIRM);
    expect(hide).not.toHaveBeenCalled();

    await button.trigger('click');
    await flushPromises();
    expect(hide).toHaveBeenCalledWith([42]);
  });

  it('родитель треда — в подтверждении число ответов, которые уйдут вместе с ним', async () => {
    vi.spyOn(store, 'hideMessages').mockResolvedValue();
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal('confirm', confirm);
    const thread = { reply_count: 5, source_reply_count: 7, last_reply_ts: 2000, unread: 0, subscribed: true };
    const parent = mount(MessageRow, { props: { message: msg({ id: 1, thread }) } });
    await parent.get('button.hide-button').trigger('click');
    expect(confirm).toHaveBeenLastCalledWith(
      'Удалить сообщение и 5 ответов в треде из хаба? В Telegram/Slack/Mattermost они останутся.',
    );
    const one = mount(MessageRow, { props: { message: msg({ id: 2, thread: { ...thread, reply_count: 1 } }) } });
    await one.get('button.hide-button').trigger('click');
    expect(confirm).toHaveBeenLastCalledWith(
      'Удалить сообщение и 1 ответ в треде из хаба? В Telegram/Slack/Mattermost они останутся.',
    );
    const plain = mount(MessageRow, { props: { message: msg({ id: 3 }) } });
    await plain.get('button.hide-button').trigger('click');
    expect(confirm).toHaveBeenLastCalledWith('Удалить сообщение из хаба? В Telegram/Slack/Mattermost оно останется.');
  });

  it('альбом — DELETE на каждую часть (П11)', async () => {
    const hide = vi.spyOn(store, 'hideMessages').mockResolvedValue();
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    const parts = [
      msg({ id: 1, grouped_id: 'g', attachments_json: JSON.stringify([{ type: 'photo' }]) }),
      msg({ id: 2, grouped_id: 'g', attachments_json: JSON.stringify([{ type: 'photo' }]) }),
    ];
    const wrapper = mount(AlbumRow, { props: { messages: parts } });
    await wrapper.get('button.hide-button').trigger('click');
    await flushPromises();
    expect(hide).toHaveBeenCalledWith([1, 2]);
  });

  it('стор: DELETE по очереди, строка уходит из ленты и треда сразу', async () => {
    const s = createStore();
    s.state.sync.messagesByChannel[5] = [msg({ id: 1 }), msg({ id: 2, ext_id: '2', ts: 2000 })];
    const key = threadKey(5, '1');
    s.state.sync.threads[key] = {
      parent: msg({ id: 1 }),
      replies: [msg({ id: 3, ext_id: '3', thread_ext_id: '1', ts: 3000 })],
      nextBefore: null,
      loading: false,
    };
    const calls: string[] = [];
    const fetchMock = vi.fn((url: string, init: { method: string; headers: Record<string, string> }) => {
      calls.push(`${init.method} ${url} ${init.headers['X-Hub-CSRF']}`);
      if (url.startsWith('/api/v1/sync')) return Promise.resolve(jsonResponse({ messages: [], channels: [], max_seq: 0, more: false }));
      if (url.startsWith('/api/v1/status')) return Promise.resolve(jsonResponse({ max_seq: 0 }));
      return Promise.resolve(new Response(null, { status: 204 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    api.setCsrf('csrf-1');
    await s.hideMessages([1, 3]);
    expect(calls.slice(0, 2)).toEqual(['DELETE /api/v1/messages/1 csrf-1', 'DELETE /api/v1/messages/3 csrf-1']);
    expect(s.state.sync.messagesByChannel[5]!.map((m) => m.id)).toEqual([2]);
    const thread = s.state.sync.threads[key]!;
    expect(thread.replies).toEqual([]);
    expect(thread.parent?.hidden).toBe(true);
    expect(thread.parent?.text).toBeNull();
  });

  it('409 — сообщение «дождитесь итога отправки», строка остаётся', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    const alert = vi.fn();
    vi.stubGlobal('alert', alert);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(api, 'hideMessage').mockRejectedValue(new api.ApiError(409, 'дождитесь итога отправки', null));
    vi.spyOn(store, 'runSync').mockResolvedValue();
    store.state.sync.messagesByChannel[5] = [msg({ id: 7 })];
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 7 }) } });
    await wrapper.get('button.hide-button').trigger('click');
    await flushPromises();
    expect(alert.mock.calls[0]![0]).toContain('дождитесь итога отправки');
    expect(store.state.sync.messagesByChannel[5]!.map((m) => m.id)).toEqual([7]);
  });

  it('родитель треда, удалённый у себя, — заглушка без кнопок', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ hidden: true, text: null, sender: null }) },
    });
    expect(wrapper.get('.message-hidden').text()).toBe('удалено у себя');
    expect(wrapper.find('button').exists()).toBe(false);
  });
});

describe('T8: «Удалить беседу из хаба»', () => {
  it('подтверждение — DELETE беседы, отмена — ничего', async () => {
    store.state.sync.channelsById[5] = channel({});
    const hide = vi.spyOn(store, 'hideChannel').mockResolvedValue();
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal('confirm', confirm);
    const wrapper = mount(ChannelView, { props: { channelId: 5 } });
    const button = wrapper.get('button.hide-channel-button');
    await button.trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenCalledWith(HIDE_CHANNEL_CONFIRM);
    expect(hide).not.toHaveBeenCalled();
    await button.trigger('click');
    await flushPromises();
    expect(hide).toHaveBeenCalledWith(5);
  });

  it('стор: беседа пропадает из списка, открытая закрывается вместе с тредом', async () => {
    const s = createStore();
    s.state.sync.channelsById[5] = channel({});
    s.state.sync.messagesByChannel[5] = [msg({ id: 1 })];
    s.state.currentChannelId = 5;
    s.state.openThread = { channelId: 5, threadExtId: '1' };
    vi.spyOn(api, 'hideChannel').mockResolvedValue(null);
    vi.spyOn(s, 'runSync').mockResolvedValue();
    await s.hideChannel(5);
    expect(s.state.sync.channelsById[5]).toBeUndefined();
    expect(s.state.sync.messagesByChannel[5]).toBeUndefined();
    expect(s.state.currentChannelId).toBeNull();
    expect(s.state.openThread).toBeNull();
  });
});

describe('T9: `hidden: true` из /sync', () => {
  it('убирает сообщение из ленты и из ответов треда; родитель треда — заглушкой', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [msg({ id: 1 }), msg({ id: 2, ext_id: '2', ts: 2000 })],
      channels: [],
      max_seq: 2,
    });
    const key = threadKey(5, '1');
    state.threads[key] = {
      parent: msg({ id: 1 }),
      replies: [msg({ id: 3, ext_id: '3', thread_ext_id: '1', ts: 3000 })],
      nextBefore: null,
      loading: false,
    };
    const stub = { id: 2, seq: 5, channel_id: 5, project_id: 1, thread_ext_id: null, hidden: true } as unknown as Message;
    const replyStub = { id: 3, seq: 6, channel_id: 5, project_id: 1, thread_ext_id: '1', hidden: true } as unknown as Message;
    const parentStub = { id: 1, seq: 7, channel_id: 5, project_id: 1, thread_ext_id: null, hidden: true } as unknown as Message;
    applySync(state, { messages: [stub, replyStub, parentStub], channels: [], max_seq: 7 });
    expect(state.messagesByChannel[5]).toEqual([]);
    expect(state.threads[key]!.replies).toEqual([]);
    expect(state.threads[key]!.parent).toMatchObject({ id: 1, hidden: true, text: null, sender: null });
    expect(state.lastSeq).toBe(7);
  });

  it('беседа с `hidden: true` пропадает; без него — возвращается', () => {
    const state = createSyncState();
    applySync(state, { messages: [msg({ id: 1 })], channels: [channel({})], max_seq: 1 });
    applySync(state, { messages: [], channels: [channel({ hidden: true })], max_seq: 2 });
    expect(state.channelsById[5]).toBeUndefined();
    expect(state.messagesByChannel[5]).toBeUndefined();
    applySync(state, { messages: [msg({ id: 9, ext_id: '9' })], channels: [channel({ hidden: false })], max_seq: 3 });
    expect(state.channelsById[5]).toBeDefined();
    expect(state.messagesByChannel[5]!.map((m) => m.id)).toEqual([9]);
  });

  it('стор: открытая беседа, удалённая в другой вкладке, закрывается по /sync', async () => {
    const s = createStore();
    s.state.sync.channelsById[5] = channel({});
    s.state.currentChannelId = 5;
    const res: SyncResponse = { messages: [], channels: [channel({ hidden: true })], max_seq: 10, more: false };
    vi.spyOn(api, 'sync').mockResolvedValue(res);
    vi.spyOn(api, 'getStatus').mockResolvedValue({ max_seq: 10 } as never);
    vi.spyOn(api, 'listProjects').mockResolvedValue([]);
    await s.runSync();
    expect(s.state.currentChannelId).toBeNull();
    expect(s.state.sync.channelsById[5]).toBeUndefined();
  });
});

describe('T10: «изменено» раскрывает прежние версии', () => {
  it('клик — версии от старой к новой; без версий пометка не кликабельна', async () => {
    const load = vi.spyOn(store, 'loadEdits').mockResolvedValue([
      { replaced_ts: 2000, text: 'версия раз', entities_json: null },
      { replaced_ts: 3000, text: 'версия два', entities_json: null },
    ]);
    const wrapper = mount(MessageRow, {
      props: { message: msg({ id: 11, text: 'сейчас', edited_ts: 3000, edit_count: 2 }) },
    });
    expect(wrapper.find('.edit-history').exists()).toBe(false);
    await wrapper.get('button.edited-toggle').trigger('click');
    await flushPromises();
    expect(load).toHaveBeenCalledWith(11);
    const versions = wrapper.findAll('.edit-version').map((v) => v.text());
    expect(versions).toHaveLength(2);
    expect(versions[0]).toContain('версия раз');
    expect(versions[1]).toContain('версия два');
    await wrapper.get('button.edited-toggle').trigger('click');
    expect(wrapper.find('.edit-history').exists()).toBe(false);

    const plain = mount(MessageRow, { props: { message: msg({ edited_ts: 3000, edit_count: 0 }) } });
    expect(plain.find('button.edited-toggle').exists()).toBe(false);
    expect(plain.get('.edited-mark').text()).toBe('изменено');
  });

  it('API: GET /messages/{id}/edits', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ message_id: 3, edits: [] }));
    vi.stubGlobal('fetch', fetchMock);
    await api.listMessageEdits(3);
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/messages/3/edits');
  });
});
