// Ф14-7 (`docs/architecture.md`, тесты 6–7): метка «K новых» у сводки треда и прокрутка к
// родителю треда, когда непрочитанное беседы только в тредах.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Channel, Message, ThreadSummary } from '../src/api/types';
import MessageFeed from '../src/components/MessageFeed.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { store } from '../src/store/instance';

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'D1',
    kind: 'dm',
    title: 'Собеседник',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 1,
    source: 'slack',
    ...overrides,
  };
}

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
    sender: null,
    ...overrides,
  };
}

function thread(overrides: Partial<ThreadSummary>): ThreadSummary {
  return { reply_count: 1, source_reply_count: null, last_reply_ts: 2000, unread: 0, subscribed: true, ...overrides };
}

const originalScroll = Element.prototype.scrollIntoView;

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  store.state.focus = null;
});

afterEach(() => {
  Element.prototype.scrollIntoView = originalScroll;
  vi.restoreAllMocks();
});

describe('Ф14-7 B1: метка «K новых» у сводки треда', () => {
  it('подписан и есть новые — метка; красная, если у беседы есть «реагирую»', () => {
    store.state.sync.channelsById[1] = channel({ unread_react: 1 });
    const wrapper = mount(MessageRow, {
      props: { message: msg({ ext_id: 'P1', thread: thread({ reply_count: 2, unread: 1 }) }) },
    });
    const badge = wrapper.get('.thread-summary-new');
    expect(badge.text()).toBe('1 новых');
    expect(badge.classes()).toContain('badge-react');
    expect(wrapper.get('.thread-summary').text()).not.toContain('новых');
  });

  it('без «реагирую» у беседы — серая метка', () => {
    store.state.sync.channelsById[1] = channel({ unread_react: 0 });
    const wrapper = mount(MessageRow, {
      props: { message: msg({ ext_id: 'P1', thread: thread({ unread: 3 }) }) },
    });
    const badge = wrapper.get('.thread-summary-new');
    expect(badge.classes()).toContain('badge-quiet');
    expect(badge.classes()).not.toContain('badge-react');
  });

  it('нет непрочитанного или тред не подписан — метки нет', () => {
    const read = mount(MessageRow, { props: { message: msg({ ext_id: 'P1', thread: thread({ unread: 0 }) }) } });
    expect(read.find('.thread-summary-new').exists()).toBe(false);
    const unsubscribed = mount(MessageRow, {
      props: { message: msg({ ext_id: 'P2', thread: thread({ unread: 2, subscribed: false }) }) },
    });
    expect(unsubscribed.find('.thread-summary-new').exists()).toBe(false);
  });

  it('клик по метке открывает тред', async () => {
    const spy = vi.spyOn(store, 'openThread').mockResolvedValue();
    const wrapper = mount(MessageRow, {
      props: { message: msg({ channel_id: 1, ext_id: 'P1', thread: thread({ unread: 1 }) }) },
    });
    await wrapper.get('.thread-summary-new').trigger('click');
    expect(spy).toHaveBeenCalledWith(1, 'P1');
  });
});

describe('Ф14-7 B2: беседа с непрочитанным только в тредах открывается на родителе', () => {
  function feedMessages(): Message[] {
    return [
      msg({ id: 10, ext_id: 'A', ts: 1000 }),
      msg({ id: 11, ext_id: 'P1', ts: 1100, is_me: true, thread: thread({ unread: 1 }) }),
      msg({ id: 12, ext_id: 'P2', ts: 1200, is_me: true, thread: thread({ unread: 2 }) }),
      msg({ id: 13, ext_id: 'B', ts: 1300 }),
    ];
  }

  function spyScroll(): number[] {
    const scrolled: number[] = [];
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(Number(this.getAttribute('data-message-id')));
    };
    return scrolled;
  }

  it('unread_top 0, unread_threads > 0 — прокрутка к родителю самого раннего треда и подсветка', async () => {
    const scrolled = spyScroll();
    store.state.sync.channelsById[1] = channel({ unread_top: 0, unread_threads: 3 });
    store.state.sync.messagesByChannel[1] = feedMessages();
    const wrapper = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    await flushPromises();
    expect(scrolled).toEqual([11]);
    expect(wrapper.get('[data-message-id="11"]').classes()).toContain('message-row--focus');
    wrapper.unmount();
  });

  it('с главной: цель — ответ в треде (в ленте его нет) — прокрутка к родителю', async () => {
    const scrolled = spyScroll();
    store.state.sync.channelsById[1] = channel({ unread_top: 0, unread_threads: 3 });
    store.state.sync.messagesByChannel[1] = feedMessages();
    store.state.focus = { channelId: 1, messageId: 99, outboxId: null, seq: 1 };
    const wrapper = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    await flushPromises();
    expect(scrolled).toEqual([11]);
    wrapper.unmount();
  });

  it('с главной: цель в ленте — прокрутка к цели, не к родителю', async () => {
    const scrolled = spyScroll();
    store.state.sync.channelsById[1] = channel({ unread_top: 0, unread_threads: 3 });
    store.state.sync.messagesByChannel[1] = feedMessages();
    store.state.focus = { channelId: 1, messageId: 13, outboxId: null, seq: 1 };
    const wrapper = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    await flushPromises();
    expect(scrolled).toEqual([13]);
    wrapper.unmount();
  });

  it('есть непрочитанное верхнего уровня — без прокрутки', async () => {
    const scrolled = spyScroll();
    store.state.sync.channelsById[1] = channel({ unread_top: 1, unread_threads: 3 });
    store.state.sync.messagesByChannel[1] = feedMessages();
    const wrapper = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    await flushPromises();
    expect(scrolled).toEqual([]);
    wrapper.unmount();
  });

  it('родителя нет в загруженной странице — без прокрутки', async () => {
    const scrolled = spyScroll();
    store.state.sync.channelsById[1] = channel({ unread_top: 0, unread_threads: 1 });
    store.state.sync.messagesByChannel[1] = [msg({ id: 10, ext_id: 'A' }), msg({ id: 13, ext_id: 'B', ts: 1300 })];
    const wrapper = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    await flushPromises();
    expect(scrolled).toEqual([]);
    wrapper.unmount();
  });

  it('сообщения пришли после открытия — прокрутка на первой пачке, один раз', async () => {
    const scrolled = spyScroll();
    store.state.sync.channelsById[1] = channel({ unread_top: 0, unread_threads: 1 });
    const wrapper = mount(MessageFeed, { props: { channelId: 1 }, attachTo: document.body });
    await flushPromises();
    expect(scrolled).toEqual([]);
    store.state.sync.messagesByChannel[1] = feedMessages();
    await flushPromises();
    await flushPromises();
    expect(scrolled).toEqual([11]);
    store.state.sync.messagesByChannel[1] = [...feedMessages(), msg({ id: 14, ext_id: 'C', ts: 1400 })];
    await flushPromises();
    await flushPromises();
    expect(scrolled).toEqual([11]);
    wrapper.unmount();
  });
});
