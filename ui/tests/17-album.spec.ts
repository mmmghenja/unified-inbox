// Спека 1d-1, п.2: альбом — подряд идущие сообщения одного отправителя с одинаковым `grouped_id` — одна
// строка ленты: общая подпись (первая непустая) + вложения всех сообщений. Правка/удаление любого из них
// обновляет строку. Группировка — чистая функция `store/feedRows.ts`; здесь же — сквозная проверка через
// смонтированный `MessageFeed`.
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import MessageFeed from '../src/components/MessageFeed.vue';
import { buildFeedRows } from '../src/store/feedRows';
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
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'file',
    text: null,
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
    sender: { id: 7, name: 'Аня', handle: null },
    ...overrides,
  };
}

describe('17 feedRows (чистая функция): группировка альбома', () => {
  it('три сообщения подряд, один grouped_id, один отправитель -> одна строка "album"', () => {
    const messages = [
      msg({ id: 1, ts: 1000, grouped_id: 'g1' }),
      msg({ id: 2, ts: 1001, grouped_id: 'g1', text: 'подпись' }),
      msg({ id: 3, ts: 1002, grouped_id: 'g1' }),
    ];
    const rows = buildFeedRows(messages);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.kind).toBe('album');
    if (rows[0]!.kind === 'album') {
      expect(rows[0]!.messages.map((m) => m.id)).toEqual([1, 2, 3]);
    }
  });

  it('разные отправители с тем же grouped_id — НЕ схлопываются', () => {
    const messages = [
      msg({ id: 1, ts: 1000, grouped_id: 'g1', sender: { id: 1, name: 'A', handle: null } }),
      msg({ id: 2, ts: 1001, grouped_id: 'g1', sender: { id: 2, name: 'B', handle: null } }),
    ];
    const rows = buildFeedRows(messages);
    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.kind === 'single')).toBe(true);
  });

  it('одиночное сообщение без grouped_id — отдельная строка "single"', () => {
    const rows = buildFeedRows([msg({ id: 1, grouped_id: null })]);
    expect(rows).toEqual([{ kind: 'single', message: expect.objectContaining({ id: 1 }) }]);
  });

  it('несмежные сообщения с тем же grouped_id (между ними чужое) — два отдельных альбома/строки', () => {
    const messages = [
      msg({ id: 1, ts: 1000, grouped_id: 'g1' }),
      msg({ id: 2, ts: 1001, grouped_id: 'other', sender: { id: 99, name: 'Чужой', handle: null } }),
      msg({ id: 3, ts: 1002, grouped_id: 'g1' }),
    ];
    const rows = buildFeedRows(messages);
    expect(rows).toHaveLength(3);
  });
});

describe('17 MessageFeed: альбом рендерится одной строкой, обновляется при удалении одного из сообщений', () => {
  beforeEach(() => {
    store.state.sync.messagesByChannel = {};
    store.state.loadingMessagesFor = {};
  });

  it('альбом из трёх сообщений -> одна строка ленты с общей подписью и всеми вложениями', () => {
    const channelId = 101;
    store.state.sync.messagesByChannel[channelId] = [
      msg({
        id: 1,
        ts: 1000,
        grouped_id: 'g1',
        attachments_json: JSON.stringify([{ type: 'photo', name: 'a.jpg' }]),
      }),
      msg({
        id: 2,
        ts: 1001,
        grouped_id: 'g1',
        text: 'подпись альбома',
        attachments_json: JSON.stringify([{ type: 'photo', name: 'b.jpg' }]),
      }),
      msg({
        id: 3,
        ts: 1002,
        grouped_id: 'g1',
        attachments_json: JSON.stringify([{ type: 'photo', name: 'c.jpg' }]),
      }),
    ];

    const wrapper = mount(MessageFeed, { props: { channelId } });
    const rows = wrapper.findAll('li.message-row');
    expect(rows).toHaveLength(1);
    expect(rows[0]!.text()).toContain('подпись альбома');
    expect(wrapper.findAll('.attachment')).toHaveLength(3);
  });

  it('удаление сообщения с подписью — строка остаётся одна, подпись переходит на следующее валидное', async () => {
    const channelId = 102;
    const messages = [
      msg({ id: 10, ts: 1000, grouped_id: 'g2', text: 'первая подпись' }),
      msg({ id: 11, ts: 1001, grouped_id: 'g2', text: 'вторая подпись' }),
    ];
    store.state.sync.messagesByChannel[channelId] = messages;

    const wrapper = mount(MessageFeed, { props: { channelId } });
    expect(wrapper.findAll('li.message-row')).toHaveLength(1);
    expect(wrapper.text()).toContain('первая подпись');

    // "удаление" первого сообщения альбома через тот же приём, что применяет applySync (upsert по id).
    store.state.sync.messagesByChannel[channelId] = [
      { ...messages[0]!, text: null, deleted: true },
      messages[1]!,
    ];
    await wrapper.vm.$nextTick();

    expect(wrapper.findAll('li.message-row')).toHaveLength(1);
    expect(wrapper.text()).not.toContain('первая подпись');
    expect(wrapper.text()).toContain('вторая подпись');
  });

  it('удаление ВСЕХ сообщений альбома — одна строка «сообщение удалено»', async () => {
    const channelId = 103;
    const messages = [
      msg({ id: 20, ts: 1000, grouped_id: 'g3', text: 'a' }),
      msg({ id: 21, ts: 1001, grouped_id: 'g3', text: 'b' }),
    ];
    store.state.sync.messagesByChannel[channelId] = messages;
    const wrapper = mount(MessageFeed, { props: { channelId } });

    store.state.sync.messagesByChannel[channelId] = messages.map((m) => ({ ...m, text: null, deleted: true }));
    await wrapper.vm.$nextTick();

    const rows = wrapper.findAll('li.message-row');
    expect(rows).toHaveLength(1);
    expect(rows[0]!.text()).toContain('сообщение удалено');
  });
});
