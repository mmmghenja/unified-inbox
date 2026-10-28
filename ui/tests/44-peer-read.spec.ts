// Ф11-7 (`docs/architecture.md`, тест 4): у моего отправленного сообщения Telegram —
// «✓✓», если его позиция не дальше `peer_read_sort` беседы, иначе «✓»; заготовка — только статус outbox;
// беседа Slack — без отметок.
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';

import type { Channel, Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { store } from '../src/store/instance';
import { peerReadMark } from '../src/util/peerRead';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 1,
    ext_id: '10',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: true,
    kind: 'text',
    text: 'моё',
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
    my_msg_count: 1,
    first_seen_ts: 0,
    unread: 0,
    project_id: 1,
    source: 'telegram',
    peer_read_sort: 10,
    ...overrides,
  };
}

function mark(wrapper: ReturnType<typeof mount>): string | null {
  const el = wrapper.find('.peer-read-mark');
  return el.exists() ? el.text() : null;
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
});

describe('Ф11-7: «✓»/«✓✓» у моих сообщений', () => {
  it('ext_sort не дальше маркера — «✓✓», дальше — «✓»', () => {
    store.state.sync.channelsById = { 5: channel({}) };
    const read = mount(MessageRow, { props: { message: msg({ ext_id: '10' }) } });
    expect(mark(read)).toBe('✓✓');
    expect(read.get('.peer-read-mark').attributes('title')).toBe('Прочитано');
    const older = mount(MessageRow, { props: { message: msg({ id: 2, ext_id: '9' }) } });
    expect(mark(older)).toBe('✓✓');
    const newer = mount(MessageRow, { props: { message: msg({ id: 3, ext_id: '11' }) } });
    expect(mark(newer)).toBe('✓');
  });

  it('маркера ещё нет у беседы Telegram — «✓»; в группе ✓✓ подписано «кем-то»', () => {
    store.state.sync.channelsById = { 5: channel({ peer_read_sort: null }) };
    expect(mark(mount(MessageRow, { props: { message: msg({}) } }))).toBe('✓');
    store.state.sync.channelsById = { 5: channel({ kind: 'group' }) };
    const w = mount(MessageRow, { props: { message: msg({}) } });
    expect(mark(w)).toBe('✓✓');
    expect(w.get('.peer-read-mark').attributes('title')).toContain('кем-то');
  });

  it('заготовка — статус outbox, без отметки; отправленная (sent) с ext_id — с отметкой', () => {
    store.state.sync.channelsById = { 5: channel({}) };
    const draft = mount(MessageRow, {
      props: { message: msg({ ext_id: null, outbox_id: 7, outbox_status: 'pending_approval' }) },
    });
    expect(mark(draft)).toBeNull();
    expect(draft.find('.peer-read-mark').exists()).toBe(false);
    const sent = mount(MessageRow, {
      props: { message: msg({ ext_id: '10', outbox_id: 8, outbox_status: 'sent' }) },
    });
    expect(mark(sent)).toBe('✓✓');
  });

  it('беседа Slack — без отметок; чужое сообщение — без отметки', () => {
    store.state.sync.channelsById = {
      5: channel({ source: 'slack', peer_read_sort: null, kind: 'group' }),
    };
    expect(mark(mount(MessageRow, { props: { message: msg({ ext_id: '1700000000.000100' }) } }))).toBeNull();
    store.state.sync.channelsById = { 5: channel({}) };
    expect(mark(mount(MessageRow, { props: { message: msg({ is_me: false }) } }))).toBeNull();
  });

  it('peerReadMark: ленты и темы, удалённое и не-Telegram без маркера — null; fake с маркером — да', () => {
    const m = msg({});
    expect(peerReadMark(m, undefined)).toBeNull();
    expect(peerReadMark(m, channel({ kind: 'channel' }))).toBeNull();
    expect(peerReadMark(m, channel({ kind: 'topic' }))).toBeNull();
    expect(peerReadMark(msg({ deleted: true }), channel({}))).toBeNull();
    expect(peerReadMark(m, channel({ source: 'mail', peer_read_sort: null }))).toBeNull();
    expect(peerReadMark(m, channel({ source: 'fake', peer_read_sort: 10 }))).toBe('read');
    expect(peerReadMark(msg({ outbox_id: 1, outbox_status: 'unknown' }), channel({}))).toBeNull();
  });
});
