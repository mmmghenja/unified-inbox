// Ф15-1 D (`docs/architecture.md`): реакция на письмо — кнопка только у
// письма с `can_react`, быстрый ряд почты, чип «ставится» и отмена повторным кликом в окне 10 с; после окна и у
// ушедшей реакции — запроса нет, строка «в письме реакцию после отправки не снять».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../src/api/client';
import type { Channel, Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { QUICK_REACTIONS } from '../src/render/reactions';
import { store } from '../src/store/instance';
import { reactionStates } from '../src/store/reactions';

function letter(overrides: Partial<Message>): Message {
  return {
    id: 7,
    seq: 1,
    first_seq: 1,
    channel_id: 9,
    project_id: 1,
    ext_id: '<a@gmail.com>',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'Добрый день',
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
    sender: { id: 1, name: 'Пётр', handle: 'petr@gmail.com' },
    ...overrides,
  };
}

function mailChannel(): Channel {
  return {
    id: 9,
    ext_id: '123',
    kind: 'mail_thread',
    title: 'Письмо',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 0,
    unread: 0,
    project_id: 1,
    source: 'mail',
  };
}

async function pick(wrapper: ReturnType<typeof mount>, emoji: string): Promise<void> {
  await wrapper.get('button.react-button').trigger('click');
  const button = wrapper.findAll('button.reaction-pick').find((b) => b.text() === emoji);
  expect(button, `нет «${emoji}» в быстром ряду`).toBeDefined();
  await button!.trigger('click');
  await flushPromises();
}

beforeEach(() => {
  store.state.sync.channelsById = { 9: mailChannel() };
  store.state.sync.messagesByChannel = {};
  for (const key of Object.keys(reactionStates)) delete reactionStates[Number(key)];
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('Ф15-1 D: реакция на письмо', () => {
  it('кнопка — только у письма с can_react; подсказка про «не снять»; ряд почты', async () => {
    for (const message of [letter({}), letter({ can_react: false })]) {
      expect(mount(MessageRow, { props: { message } }).find('button.react-button').exists()).toBe(false);
    }
    const wrapper = mount(MessageRow, { props: { message: letter({ can_react: true }) } });
    const button = wrapper.get('button.react-button');
    expect(button.attributes('title')).toContain('в письме реакцию после отправки не снять');
    await button.trigger('click');
    expect(wrapper.findAll('button.reaction-pick').map((b) => b.text())).toEqual([...QUICK_REACTIONS.mail]);
    expect(QUICK_REACTIONS.mail).toEqual(['👍', '❤️', '😂', '🙏', '🔥', '👀']);
  });

  it('клик — «ставится»; повторный клик по чипу в окне 10 с — отмена (off)', async () => {
    vi.useFakeTimers();
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: letter({ can_react: true }) } });
    await pick(wrapper, '👍');
    expect(set).toHaveBeenLastCalledWith(7, '👍', true);
    const chip = wrapper.get('button.reaction--button');
    expect(chip.classes()).toContain('pending');
    expect(chip.text()).toBe('👍 1');

    vi.advanceTimersByTime(9_000);
    await chip.trigger('click');
    await flushPromises();
    expect(set).toHaveBeenLastCalledWith(7, '👍', false);
    expect(wrapper.find('.reaction').exists()).toBe(false);
    expect(wrapper.find('.reaction-error').exists()).toBe(false);
  });

  it('после окна 10 с повторный клик запроса не шлёт — «не снять»', async () => {
    vi.useFakeTimers();
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: letter({ can_react: true }) } });
    await pick(wrapper, '🔥');
    vi.advanceTimersByTime(10_000);
    await wrapper.get('button.reaction--button').trigger('click');
    await flushPromises();
    expect(set).toHaveBeenCalledTimes(1);
    expect(wrapper.get('.reaction-error').text()).toBe('в письме реакцию после отправки не снять');
  });

  it('своя ушедшая реакция: клик по чипу — без запроса, «не снять»; чужую — плюсануть', async () => {
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const json = JSON.stringify([
      { emoji: '👍', count: 1, me: true },
      { emoji: '🙏', count: 2 },
    ]);
    const wrapper = mount(MessageRow, { props: { message: letter({ can_react: true, reactions_json: json }) } });
    const chips = wrapper.findAll('button.reaction--button');
    await chips[0]!.trigger('click');
    await flushPromises();
    expect(set).not.toHaveBeenCalled();
    expect(wrapper.get('.reaction-error').text()).toBe('в письме реакцию после отправки не снять');
    await chips[1]!.trigger('click');
    await flushPromises();
    expect(set).toHaveBeenCalledWith(7, '🙏', true);
  });

  it('отказ ядра «адресат не на Gmail» — понятная строка под письмом', async () => {
    vi.spyOn(store, 'setReaction').mockRejectedValue(new ApiError(409, 'not google', null));
    const wrapper = mount(MessageRow, { props: { message: letter({ can_react: true }) } });
    await pick(wrapper, '👀');
    expect(wrapper.get('.reaction-error').text()).toBe('адресат не на Gmail — реакцией не ответить');
    expect(wrapper.find('.reaction').exists()).toBe(false);
  });
});
