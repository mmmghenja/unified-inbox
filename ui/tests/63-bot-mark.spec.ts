// Ф20-1 A3 (`docs/architecture.md`): у сообщения бота — мелкая приглушённая
// метка «бот» у имени отправителя; у человека и у старого ядра (поля нет) — метки нет.
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { store } from '../src/store/instance';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
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
    text: 'деплой готов',
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
    sender: { id: 1, name: 'deploy-bot', handle: null },
    ...overrides,
  };
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
});

describe('Ф20-1: метка «бот»', () => {
  it('сообщение бота — метка рядом с именем', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ is_bot: true }) } });
    const mark = wrapper.find('.message-meta .bot-mark');
    expect(mark.exists()).toBe(true);
    expect(mark.text()).toBe('бот');
    expect(wrapper.find('.sender-name').text()).toBe('deploy-bot');
  });

  it('человек и старое ядро без поля — метки нет', () => {
    expect(mount(MessageRow, { props: { message: msg({ is_bot: false }) } }).find('.bot-mark').exists()).toBe(
      false,
    );
    expect(mount(MessageRow, { props: { message: msg({}) } }).find('.bot-mark').exists()).toBe(false);
  });
});
