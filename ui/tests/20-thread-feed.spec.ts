// Спека `docs/architecture.md`. Группа 2: сводка треда под родителем в ленте. Группа 8:
// `purged` и `thread_broadcast` — отрисовка.
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

beforeEach(() => {
  store.state.sync.channelsById = {};
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('20 п.2 Сводка треда под родителем: N = max(reply_count, source_reply_count)', () => {
  it('source_reply_count больше reply_count — показывается source_reply_count', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          ext_id: 'P1',
          thread: { reply_count: 2, source_reply_count: 5, last_reply_ts: 2000, unread: 0, subscribed: false },
        }),
      },
    });
    expect(wrapper.find('.thread-summary').text()).toContain('5');
  });

  it('source_reply_count отсутствует (null) — берётся reply_count', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          ext_id: 'P1',
          thread: { reply_count: 3, source_reply_count: null, last_reply_ts: null, unread: 0, subscribed: false },
        }),
      },
    });
    expect(wrapper.find('.thread-summary').text()).toContain('3');
  });

  it('«K новых» — только когда subscribed И unread > 0', () => {
    const subscribedUnread = mount(MessageRow, {
      props: {
        message: msg({
          ext_id: 'P1',
          thread: { reply_count: 3, source_reply_count: null, last_reply_ts: null, unread: 2, subscribed: true },
        }),
      },
    });
    // Ф14-7 B1: «K новых» — отдельной меткой рядом со сводкой, не в её тексте.
    expect(subscribedUnread.find('.thread-summary').text()).not.toContain('новых');
    expect(subscribedUnread.find('.thread-summary-new').text()).toBe('2 новых');

    const notSubscribed = mount(MessageRow, {
      props: {
        message: msg({
          ext_id: 'P2',
          thread: { reply_count: 3, source_reply_count: null, last_reply_ts: null, unread: 2, subscribed: false },
        }),
      },
    });
    expect(notSubscribed.find('.thread-summary').text()).not.toContain('новых');
    expect(notSubscribed.find('.thread-summary-new').exists()).toBe(false);

    const zeroUnread = mount(MessageRow, {
      props: {
        message: msg({
          ext_id: 'P3',
          thread: { reply_count: 3, source_reply_count: null, last_reply_ts: null, unread: 0, subscribed: true },
        }),
      },
    });
    expect(zeroUnread.find('.thread-summary').text()).not.toContain('новых');
    expect(zeroUnread.find('.thread-summary-new').exists()).toBe(false);
  });

  it('нет сводки (thread=null) — строки сводки нет вовсе', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ thread: null }) } });
    expect(wrapper.find('.thread-summary').exists()).toBe(false);
  });

  it('клик по сводке -> store.openThread(channel_id, ext_id родителя)', async () => {
    const spy = vi.spyOn(store, 'openThread').mockResolvedValue();
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          channel_id: 5,
          ext_id: 'P1',
          thread: { reply_count: 1, source_reply_count: null, last_reply_ts: null, unread: 0, subscribed: false },
        }),
      },
    });
    await wrapper.find('.thread-summary').trigger('click');
    expect(spy).toHaveBeenCalledWith(5, 'P1');
  });
});

describe('20 п.8 purged: вместо текста серым «текст удалён по сроку хранения (90 дней)»', () => {
  it('purged=true, text=null -> сообщение о ретенции, не «сообщение удалено»', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ purged: true, text: null, deleted: false }) } });
    expect(wrapper.find('.message-purged').text()).toBe('текст удалён по сроку хранения (90 дней)');
    expect(wrapper.find('.message-deleted').exists()).toBe(false);
  });

  it('deleted побеждает purged (обычное «сообщение удалено»)', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ purged: true, deleted: true, text: null }) },
    });
    expect(wrapper.find('.message-deleted').text()).toBe('сообщение удалено');
    expect(wrapper.find('.message-purged').exists()).toBe(false);
  });

  it('purged=false — обычный текст рендерится как раньше', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ purged: false, text: 'обычный текст' }) } });
    expect(wrapper.find('.message-purged').exists()).toBe(false);
    expect(wrapper.text()).toContain('обычный текст');
  });
});

describe('20 п.8 thread_broadcast: пометка «из треда» в ленте, клик открывает тред', () => {
  it('пометка показана только у broadcast-сообщений', () => {
    const broadcast = mount(MessageRow, { props: { message: msg({ thread_broadcast: true }) } });
    expect(broadcast.find('.thread-broadcast-tag').exists()).toBe(true);

    const plain = mount(MessageRow, { props: { message: msg({ thread_broadcast: false }) } });
    expect(plain.find('.thread-broadcast-tag').exists()).toBe(false);
  });

  it('клик по пометке -> store.openThread(channel_id, thread_ext_id)', async () => {
    const spy = vi.spyOn(store, 'openThread').mockResolvedValue();
    const wrapper = mount(MessageRow, {
      props: { message: msg({ channel_id: 9, ext_id: 'B1', thread_ext_id: 'P1', thread_broadcast: true }) },
    });
    await wrapper.find('.thread-broadcast-tag').trigger('click');
    expect(spy).toHaveBeenCalledWith(9, 'P1');
  });
});
