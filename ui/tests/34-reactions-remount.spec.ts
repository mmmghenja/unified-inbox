// Ревью Ф10-3: реакция поставилась, пока строка сообщения была размонтирована (владелец ушёл в другую беседу и
// вернулся) — `watch` без `immediate` не сверяет операцию с правдой, через 60 с под сообщением «не поставилось»,
// хотя 👍 уже стоит.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import type { Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { store } from '../src/store/instance';
import { reactionStates } from '../src/store/reactions';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1, seq: 1, first_seq: 1, channel_id: 5, project_id: 1, ext_id: '10', thread_ext_id: null,
    thread_broadcast: false, purged: false, thread: null, reply_to_ext_id: null, grouped_id: null, is_me: false,
    kind: 'text', text: 'привет', entities_json: null, attachments_json: null, forward_json: null,
    mentions_me: false, refs_json: null, reactions_json: null, ts: 1000, edited_ts: null, deleted: false,
    outbox_id: null, outbox_status: null, sender: { id: 1, name: 'Коллега', handle: null }, ...overrides,
  } as Message;
}

beforeEach(() => {
  store.state.sync.channelsById = {
    5: { id: 5, ext_id: 'c5', kind: 'dm', title: 'Б', tier: null, muted: false, archived: false, map_reason: null,
      last_msg_ts: 1000, my_msg_count: 0, first_seen_ts: 0, unread: 0, project_id: 1, source: 'telegram' },
  } as never;
  for (const key of Object.keys(reactionStates)) delete reactionStates[Number(key)];
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it('правда пришла, пока строка была размонтирована, — «не поставилось» не показывается', async () => {
  vi.useFakeTimers();
  vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
  const first = mount(MessageRow, { props: { message: msg({}) } });
  await first.get('button.react-button').trigger('click');
  await first.findAll('button.reaction-pick').find((b) => b.text() === '👍')!.trigger('click');
  await flushPromises();
  first.unmount(); // ушёл в другую беседу
  const truth = JSON.stringify([{ emoji: '👍', count: 1, me: true }]);
  const again = mount(MessageRow, { props: { message: msg({ reactions_json: truth }) } }); // вернулся
  vi.advanceTimersByTime(60_000);
  await flushPromises();
  expect(again.find('.reaction-error').exists()).toBe(false);
});
