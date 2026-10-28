// Ф10-3 (`docs/architecture.md` и поправки П1–П3, П11): кнопка «Реакция» и
// быстрый ряд по источнику, операции `{emoji, on}` от ожидаемого состояния, чипы-кнопки, «ставится» до
// соответствия правде или 60 с («не поставилось»), строка ошибки под сообщением.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, NetworkError } from '../src/api/client';
import type { Channel, Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { QUICK_REACTIONS, parseReactions } from '../src/render/reactions';
import { store } from '../src/store/instance';
import { displayReactions, reactionStates } from '../src/store/reactions';

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

function channel(source: string | undefined): Channel {
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
    source,
  };
}

function reactionsJson(items: { emoji?: string; custom?: string; count: number; me?: boolean }[]): string {
  return JSON.stringify(items);
}

function withSource(source: string | undefined): void {
  store.state.sync.channelsById = { 5: channel(source) };
}

async function pick(wrapper: ReturnType<typeof mount>, emoji: string): Promise<void> {
  await wrapper.get('button.react-button').trigger('click');
  const button = wrapper.findAll('button.reaction-pick').find((b) => b.text() === emoji);
  expect(button, `нет «${emoji}» в быстром ряду`).toBeDefined();
  await button!.trigger('click');
  await flushPromises();
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  for (const key of Object.keys(reactionStates)) delete reactionStates[Number(key)];
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('E9: кнопка «Реакция» — только там, где можно ставить', () => {
  it('telegram и slack с ext_id — есть; почта, fake, неизвестный источник — нет', () => {
    for (const source of ['telegram', 'slack']) {
      withSource(source);
      expect(mount(MessageRow, { props: { message: msg({}) } }).find('button.react-button').exists()).toBe(true);
    }
    for (const source of ['mail', 'fake', undefined]) {
      withSource(source);
      expect(mount(MessageRow, { props: { message: msg({}) } }).find('button.react-button').exists()).toBe(false);
    }
  });

  it('без ext_id (своя заготовка), удалённое, служебное — нет', () => {
    withSource('telegram');
    for (const message of [
      msg({ ext_id: null, is_me: true, outbox_id: 3, outbox_status: 'pending_approval' }),
      msg({ deleted: true }),
      msg({ kind: 'event' }),
    ]) {
      expect(mount(MessageRow, { props: { message } }).find('button.react-button').exists()).toBe(false);
    }
  });

  it('быстрый ряд по источнику (П2): Telegram — без U+FE0F и 🤣, Slack — ❤️ и ✅', async () => {
    withSource('telegram');
    const tg = mount(MessageRow, { props: { message: msg({}) } });
    await tg.get('button.react-button').trigger('click');
    expect(tg.findAll('button.reaction-pick').map((b) => b.text())).toEqual([...QUICK_REACTIONS.telegram]);
    expect(QUICK_REACTIONS.telegram).toContain('❤');
    expect(QUICK_REACTIONS.telegram).toContain('🤣');

    withSource('slack');
    const sl = mount(MessageRow, { props: { message: msg({}) } });
    await sl.get('button.react-button').trigger('click');
    expect(sl.findAll('button.reaction-pick').map((b) => b.text())).toEqual([...QUICK_REACTIONS.slack]);
    expect(QUICK_REACTIONS.slack).toContain('❤️');
    expect(QUICK_REACTIONS.slack).toContain('✅');
  });

  it('Esc и клик мимо закрывают ряд', async () => {
    withSource('telegram');
    const wrapper = mount(MessageRow, { props: { message: msg({}) }, attachTo: document.body });
    await wrapper.get('button.react-button').trigger('click');
    expect(wrapper.find('.reaction-picker').exists()).toBe(true);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(wrapper.find('.reaction-picker').exists()).toBe(false);

    await wrapper.get('button.react-button').trigger('click');
    wrapper.get('.reaction-picker').element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    await flushPromises();
    expect(wrapper.find('.reaction-picker').exists()).toBe(true); // клик внутри — не закрывает
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    await flushPromises();
    expect(wrapper.find('.reaction-picker').exists()).toBe(false);
    wrapper.unmount();
  });
});

describe('E9 + П1: операции', () => {
  it('Telegram: выбор -> on; повтор своей -> off; выбор другой при своей -> on другой', async () => {
    withSource('telegram');
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const mine = reactionsJson([{ emoji: '👍', count: 2, me: true }]);

    await pick(mount(MessageRow, { props: { message: msg({ id: 11 }) } }), '🔥');
    expect(set).toHaveBeenLastCalledWith(11, '🔥', true);

    await pick(mount(MessageRow, { props: { message: msg({ id: 12, reactions_json: mine }) } }), '👍');
    expect(set).toHaveBeenLastCalledWith(12, '👍', false);

    // своя ❤️ из Telegram приходит без U+FE0F — в ряду тоже `❤`: это снятие, не вторая реакция
    const heart = reactionsJson([{ emoji: '❤', count: 1, me: true }]);
    await pick(mount(MessageRow, { props: { message: msg({ id: 13, reactions_json: heart }) } }), '❤');
    expect(set).toHaveBeenLastCalledWith(13, '❤', false);
  });

  it('следующая операция — от ожидаемого, а не от reactions_json', async () => {
    withSource('slack');
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 21 }) } });
    await pick(wrapper, '👀');
    await pick(wrapper, '👀'); // правда ещё не пришла — второй клик снимает
    expect(set.mock.calls).toEqual([
      [21, '👀', true],
      [21, '👀', false],
    ]);
  });

  it('Slack: чип `:custom:` — кнопка с сырым значением; своя — снять, чужая — плюсануть', async () => {
    withSource('slack');
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const json = reactionsJson([
      { emoji: ':partyparrot:', count: 2, me: true },
      { emoji: '🔥', count: 1, me: false },
    ]);
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 31, reactions_json: json }) } });
    const chips = wrapper.findAll('button.reaction--button');
    expect(chips.map((c) => c.text())).toEqual([':partyparrot: 2', '🔥 1']);
    await chips[0]!.trigger('click');
    await chips[1]!.trigger('click');
    await flushPromises();
    expect(set.mock.calls).toEqual([
      [31, ':partyparrot:', false],
      [31, '🔥', true],
    ]);
  });

  it('свой эмодзи Telegram (custom) — не кнопка; без права ставить чипы — не кнопки', () => {
    withSource('telegram');
    const json = reactionsJson([
      { custom: '5555', count: 1, me: true },
      { emoji: '👍', count: 1 },
    ]);
    const wrapper = mount(MessageRow, { props: { message: msg({ reactions_json: json }) } });
    expect(wrapper.findAll('button.reaction--button').map((b) => b.text())).toEqual(['👍 1']);
    expect(wrapper.findAll('span.reaction').map((s) => s.text())).toEqual(['◆ 1']);

    withSource('mail');
    const mail = mount(MessageRow, { props: { message: msg({ reactions_json: json }) } });
    expect(mail.findAll('button.reaction--button')).toHaveLength(0);
    expect(mail.findAll('span.reaction')).toHaveLength(2);
  });
});

describe('E9 + П3: «ставится» и «не поставилось»', () => {
  it('чип «ставится» держится до соответствия моих реакций, а не до любого изменения', async () => {
    withSource('slack');
    vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 41 }) } });
    await pick(wrapper, '🔥');
    let chip = wrapper.get('.reaction.pending');
    expect(chip.text()).toBe('🔥 1');
    expect(chip.classes()).toContain('mine');

    // изменилось, но не то: чужая реакция — «ставится» остаётся
    await wrapper.setProps({ message: msg({ id: 41, reactions_json: reactionsJson([{ emoji: '👍', count: 1 }]) }) });
    chip = wrapper.get('.reaction.pending');
    expect(chip.text()).toBe('🔥 1');

    await wrapper.setProps({
      message: msg({
        id: 41,
        reactions_json: reactionsJson([
          { emoji: '👍', count: 1 },
          { emoji: '🔥', count: 1, me: true },
        ]),
      }),
    });
    expect(wrapper.find('.reaction.pending').exists()).toBe(false);
    expect(wrapper.findAll('.reaction').map((r) => r.text())).toEqual(['👍 1', '🔥 1']);
  });

  it('Telegram: новая заменяет мою — старая «ставится» без отметки, пока не пришла правда', async () => {
    withSource('telegram');
    vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const json = reactionsJson([{ emoji: '❤', count: 3, me: true }]);
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 42, reactions_json: json }) } });
    await pick(wrapper, '🔥');
    const chips = wrapper.findAll('.reaction');
    expect(chips.map((c) => c.text())).toEqual(['❤ 2', '🔥 1']);
    expect(chips.every((c) => c.classes().includes('pending'))).toBe(true);
    expect(chips[0]!.classes()).not.toContain('mine');

    await wrapper.setProps({
      message: msg({
        id: 42,
        reactions_json: reactionsJson([
          { emoji: '❤', count: 2 },
          { emoji: '🔥', count: 1, me: true },
        ]),
      }),
    });
    expect(wrapper.find('.reaction.pending').exists()).toBe(false);
  });

  it('60 с без соответствия — «не поставилось», ещё через минуту строка исчезает', async () => {
    vi.useFakeTimers();
    withSource('slack');
    vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 43 }) } });
    await pick(wrapper, '🔥');
    vi.advanceTimersByTime(59_000);
    await flushPromises();
    expect(wrapper.find('.reaction.pending').exists()).toBe(true);
    expect(wrapper.find('.reaction-error').exists()).toBe(false);

    vi.advanceTimersByTime(1_000);
    await flushPromises();
    expect(wrapper.find('.reaction.pending').exists()).toBe(false);
    expect(wrapper.get('.reaction-error').text()).toBe('не поставилось');

    vi.advanceTimersByTime(60_000);
    await flushPromises();
    expect(wrapper.find('.reaction-error').exists()).toBe(false);
  });

  it('ревью: правда пришла в стор, пока строка размонтирована, — по сроку сверка со стором, без ошибки', async () => {
    vi.useFakeTimers();
    withSource('slack');
    vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 45 }) } });
    await pick(wrapper, '🔥');
    wrapper.unmount();
    store.state.sync.messagesByChannel = {
      5: [msg({ id: 45, reactions_json: reactionsJson([{ emoji: '🔥', count: 1, me: true }]) })],
    };
    vi.advanceTimersByTime(60_000);
    await flushPromises();
    expect(reactionStates[45]!.error).toBeNull();
    expect(reactionStates[45]!.ops).toEqual([]);
  });

  it('по сроку без правды в сторе — «не поставилось» в состоянии', async () => {
    vi.useFakeTimers();
    withSource('slack');
    vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 46 }) } });
    await pick(wrapper, '🔥');
    wrapper.unmount();
    store.state.sync.messagesByChannel = { 5: [msg({ id: 46 })] };
    vi.advanceTimersByTime(60_000);
    await flushPromises();
    expect(reactionStates[46]!.error).toBe('не поставилось');
  });

  it('«не поставилось» пропадает при следующем клике', async () => {
    vi.useFakeTimers();
    withSource('slack');
    vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 44 }) } });
    await pick(wrapper, '🔥');
    vi.advanceTimersByTime(60_000);
    await flushPromises();
    expect(wrapper.find('.reaction-error').exists()).toBe(true);
    await pick(wrapper, '👀');
    expect(wrapper.find('.reaction-error').exists()).toBe(false);
  });
});

describe('E9: ошибки — строка под сообщением', () => {
  it('409 unsupported — «в этом чате нельзя ставить реакции», отметка откатывается', async () => {
    withSource('telegram');
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    vi.spyOn(store, 'setReaction').mockRejectedValue(new ApiError(409, 'unsupported', { detail: 'unsupported' }));
    const wrapper = mount(MessageRow, { props: { message: msg({ id: 51 }) } });
    await pick(wrapper, '👍');
    expect(wrapper.get('.reaction-error').text()).toBe('в этом чате нельзя ставить реакции');
    expect(wrapper.find('.reaction').exists()).toBe(false);
  });

  it('422 и сеть — свои строки; `off` — реакции выключены', async () => {
    withSource('slack');
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const set = vi.spyOn(store, 'setReaction');

    set.mockRejectedValueOnce(new ApiError(422, 'invalid emoji', null));
    const a = mount(MessageRow, { props: { message: msg({ id: 52 }) } });
    await pick(a, '👍');
    expect(a.get('.reaction-error').text()).toBe('такую реакцию поставить нельзя');

    set.mockRejectedValueOnce(new NetworkError(new Error('down')));
    const b = mount(MessageRow, { props: { message: msg({ id: 53 }) } });
    await pick(b, '👍');
    expect(b.get('.reaction-error').text()).toBe('нет связи с хабом — реакция не отправлена');

    set.mockResolvedValueOnce('off');
    const c = mount(MessageRow, { props: { message: msg({ id: 54 }) } });
    await pick(c, '👍');
    expect(c.get('.reaction-error').text()).toBe('реакции для этого аккаунта выключены');
    expect(c.find('.reaction.pending').exists()).toBe(false);
  });
});

describe('displayReactions — чистая функция', () => {
  it('без операций — правда как есть', () => {
    const parsed = parseReactions(reactionsJson([{ emoji: '👍', count: 1, me: true }]));
    expect(displayReactions('slack', parsed, [])).toEqual([{ ...parsed[0], pending: false }]);
  });

  it('снятие своей последней — счётчик 0, «ставится», без числа в тексте', () => {
    withSource('slack');
    const parsed = parseReactions(reactionsJson([{ emoji: '👍', count: 1, me: true }]));
    const shown = displayReactions('slack', parsed, [{ id: 1, emoji: '👍', on: false, at: 0 }]);
    expect(shown).toEqual([{ label: '👍', raw: '👍', count: 0, me: false, custom: false, pending: true }]);
  });
});
