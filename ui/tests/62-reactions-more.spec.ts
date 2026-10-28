// Ф18-1: реакции шире — быстрые ряды по переписке владельца (🤝 в обоих), «+» раскрывает весь набор источника
// (Telegram — его 73, Slack — общий набор), первыми в нём — ходовые в этой беседе (Slack — и свои эмодзи
// воркспейса `:name:`), ошибка «в Telegram такой реакции нет».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../src/api/client';
import type { Channel, Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import {
  ALL_REACTIONS,
  QUICK_REACTIONS,
  channelReactions,
  moreReactionsFor,
  reactionKey,
} from '../src/render/reactions';
import { store } from '../src/store/instance';
import { reactionStates } from '../src/store/reactions';

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

function channel(source: string): Channel {
  return {
    id: 5,
    ext_id: 'c5',
    kind: 'group',
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
    source,
  };
}

function withSource(source: string, feed: Message[] = []): void {
  store.state.sync.channelsById = { 5: channel(source) };
  store.state.sync.messagesByChannel = { 5: feed };
}

const json = (items: { emoji?: string; custom?: string; count: number; me?: boolean }[]): string =>
  JSON.stringify(items);

async function openMore(wrapper: ReturnType<typeof mount>): Promise<string[]> {
  await wrapper.get('button.react-button').trigger('click');
  await wrapper.get('button.reaction-more').trigger('click');
  return wrapper.findAll('.reaction-grid button.reaction-pick').map((b) => b.text());
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  for (const key of Object.keys(reactionStates)) delete reactionStates[Number(key)];
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('быстрые ряды по переписке', () => {
  it('🤝 и 👌 в обоих; Slack — ✔️ и ✅; ряды — из полного набора', () => {
    for (const source of ['telegram', 'slack'] as const) {
      expect(QUICK_REACTIONS[source]).toContain('🤝');
      expect(QUICK_REACTIONS[source]).toContain('👌');
      for (const emoji of QUICK_REACTIONS[source]) expect(ALL_REACTIONS[source]).toContain(emoji);
    }
    expect(QUICK_REACTIONS.slack).toEqual(expect.arrayContaining(['✔️', '✅', '👀']));
    expect(ALL_REACTIONS.telegram).toHaveLength(73);
    expect(ALL_REACTIONS.telegram).not.toContain('✅');
    expect(ALL_REACTIONS.slack).toEqual(expect.arrayContaining(['🔍', '☑️', '🍊']));
  });
});

describe('«+» — весь набор', () => {
  it('Telegram: сетка = набор без быстрого ряда; выбор из сетки ставит и закрывает', async () => {
    withSource('telegram');
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 7 }) } });
    const grid = await openMore(wrapper);
    expect(grid).toHaveLength(73 - QUICK_REACTIONS.telegram.length);
    expect(grid).toContain('🤔');
    expect(grid).not.toContain('👍');
    expect(wrapper.get('button.reaction-more').attributes('aria-expanded')).toBe('true');

    await wrapper.findAll('.reaction-grid button').find((b) => b.text() === '🤔')!.trigger('click');
    await flushPromises();
    expect(set).toHaveBeenLastCalledWith(7, '🤔', true);
    expect(wrapper.find('.reaction-picker').exists()).toBe(false);

    await wrapper.get('button.react-button').trigger('click');
    expect(wrapper.find('.reaction-grid').exists()).toBe(false); // после закрытия — снова свёрнуто
  });

  it('Slack: первыми — ходовые в беседе, в т.ч. свой эмодзи `:name:` текстом; клик ставит по имени', async () => {
    withSource('slack', [
      msg({ id: 1, reactions_json: json([{ emoji: ':hot_fire:', count: 9 }, { emoji: '🍊', count: 4 }]) }),
      msg({ id: 2, reactions_json: json([{ emoji: '🍊', count: 3 }, { emoji: '👍', count: 20 }]) }),
    ]);
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 3 }) } });
    const grid = await openMore(wrapper);
    expect(grid.slice(0, 2)).toEqual([':hot_fire:', '🍊']); // 👍 — в быстром ряду, не повторяется
    expect(grid.filter((e) => e === '🍊')).toHaveLength(1);
    const named = wrapper.get('.reaction-grid button.reaction-pick--name');
    expect(named.text()).toBe(':hot_fire:');
    await named.trigger('click');
    await flushPromises();
    expect(set).toHaveBeenLastCalledWith(3, ':hot_fire:', true);
  });

  it('Telegram: ходовые — только из набора Telegram, свои эмодзи (custom) не попадают', () => {
    const used = channelReactions('telegram', [
      json([
        { emoji: '🤬', count: 5 },
        { custom: '5555', count: 50 },
        { emoji: '✅', count: 40 },
      ]),
      json([{ emoji: '❤️', count: 2 }]),
      null,
      'не json',
    ]);
    expect(used).toEqual(['🤬', '❤️']);
    const more = moreReactionsFor('telegram', used);
    expect(more[0]).toBe('🤬');
    expect(more.map(reactionKey)).not.toContain('❤'); // ❤ — в быстром ряду (сравнение без U+FE0F)
  });

  it('почта: ходовых нет, сетка — общий набор', () => {
    expect(channelReactions('mail', [json([{ emoji: '🍊', count: 3 }])])).toEqual([]);
    expect(moreReactionsFor('mail', [])).not.toContain('👍');
  });
});

describe('ошибка ядра', () => {
  it('422 `not in telegram set` — «в Telegram такой реакции нет»', async () => {
    withSource('telegram');
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    vi.spyOn(store, 'setReaction').mockRejectedValueOnce(new ApiError(422, 'not in telegram set', null));
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 9 }) } });
    await wrapper.get('button.react-button').trigger('click');
    await wrapper.findAll('button.reaction-pick').find((b) => b.text() === '🤝')!.trigger('click');
    await flushPromises();
    expect(wrapper.get('.reaction-error').text()).toBe('в Telegram такой реакции нет');
  });
});
