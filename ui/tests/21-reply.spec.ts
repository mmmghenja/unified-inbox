// Спека `docs/architecture.md`. Группа 3: «Ответить» по источнику (Slack -> тред, иначе ->
// плашка ответа + reply_to). Группа 4: цитата `reply_to_ext_id` над сообщением (цель загружена/не загружена,
// рендерится только текстом).
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Channel, Message } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
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

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'c1',
    kind: 'dm',
    title: 'Канал',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 1,
    ...overrides,
  };
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
});

describe('21 группа 3: «Ответить» — видимость кнопки', () => {
  it('обычное сообщение — кнопка есть', () => {
    expect(mount(MessageRow, { props: { message: msg({}) } }).find('.reply-button').exists()).toBe(true);
  });

  it('заготовка без ext_id (ещё не отправлена) — кнопки нет', () => {
    expect(mount(MessageRow, { props: { message: msg({ ext_id: null }) } }).find('.reply-button').exists()).toBe(
      false,
    );
  });

  it('kind=event — кнопки нет', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ kind: 'event', text: 'событие' }) } });
    expect(wrapper.find('.reply-button').exists()).toBe(false);
  });

  it('удалённое сообщение — кнопки нет', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ deleted: true, text: null }) } });
    expect(wrapper.find('.reply-button').exists()).toBe(false);
  });
});

describe('21 группа 3: «Ответить» — Slack-канал открывает тред', () => {
  it('сообщение-ответ (isReply) -> тред РОДИТЕЛЯ (thread_ext_id), не свой ext_id', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1, source: 'slack' });
    const spy = vi.spyOn(store, 'openThread').mockResolvedValue();
    const wrapper = mount(MessageRow, {
      props: { message: msg({ channel_id: 1, ext_id: 'R1', thread_ext_id: 'P1' }) },
    });
    await wrapper.find('.reply-button').trigger('click');
    expect(spy).toHaveBeenCalledWith(1, 'P1');
  });

  it('сообщение top-level (не ответ, включая родителя без сводки) -> заводит СВОЙ тред (собственный ext_id)', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1, source: 'slack' });
    const spy = vi.spyOn(store, 'openThread').mockResolvedValue();
    const wrapper = mount(MessageRow, {
      props: { message: msg({ channel_id: 1, ext_id: 'P1', thread_ext_id: null }) },
    });
    await wrapper.find('.reply-button').trigger('click');
    expect(spy).toHaveBeenCalledWith(1, 'P1');
  });

  it('Slack-канал — клик НЕ эмитит reply (плашка ответа не для Slack)', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1, source: 'slack' });
    vi.spyOn(store, 'openThread').mockResolvedValue();
    const wrapper = mount(MessageRow, { props: { message: msg({ channel_id: 1, ext_id: 'P1' }) } });
    await wrapper.find('.reply-button').trigger('click');
    expect(wrapper.emitted('reply')).toBeUndefined();
  });
});

describe('21 группа 3: «Ответить» — не-Slack канал (плашка ответа в поле ввода ленты)', () => {
  it('клик открывает плашку «Ответ <имя>: <текст>», отправка идёт с reply_to, крестик снимает режим', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1, source: 'telegram' });
    store.state.sync.messagesByChannel[1] = [
      msg({
        id: 10,
        channel_id: 1,
        ext_id: 'M1',
        text: 'исходное сообщение',
        sender: { id: 2, name: 'Настя', handle: null },
      }),
    ];
    const sendSpy = vi.spyOn(store, 'send').mockResolvedValue();

    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await wrapper.find('.reply-button').trigger('click');
    expect(wrapper.find('.composer-reply-banner').text()).toContain('Ответ Настя: исходное сообщение');

    await wrapper.find('textarea').setValue('мой ответ');
    await wrapper.find('form').trigger('submit');
    await new Promise((resolve) => setTimeout(resolve, 0));
    await wrapper.vm.$nextTick();

    expect(sendSpy).toHaveBeenCalledWith(1, 'мой ответ', expect.any(String), {
      thread: undefined,
      replyTo: 'M1',
    });
    // после успешной отправки ответа плашка снимается сама (спека: следующее сообщение — уже не ответ).
    expect(wrapper.find('.composer-reply-banner').exists()).toBe(false);
  });

  it('крестик отменяет режим ответа без отправки', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1, source: 'telegram' });
    store.state.sync.messagesByChannel[1] = [
      msg({ id: 10, channel_id: 1, ext_id: 'M1', text: 'x', sender: null }),
    ];
    vi.spyOn(store, 'send').mockResolvedValue();

    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await wrapper.find('.reply-button').trigger('click');
    expect(wrapper.find('.composer-reply-banner').exists()).toBe(true);
    await wrapper.find('.composer-reply-cancel').trigger('click');
    expect(wrapper.find('.composer-reply-banner').exists()).toBe(false);
  });

  it('без source вовсе (старое ядро) — тоже ветка плашки ответа, не Slack', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1 }); // source не задан
    store.state.sync.messagesByChannel[1] = [msg({ id: 10, channel_id: 1, ext_id: 'M1', text: 'x' })];
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await wrapper.find('.reply-button').trigger('click');
    expect(wrapper.find('.composer-reply-banner').exists()).toBe(true);
  });
});

describe('21 группа 4: цитата reply_to — цель загружена / не загружена', () => {
  it('цель загружена среди сообщений канала — имя и начало текста', () => {
    store.state.sync.messagesByChannel[1] = [
      msg({ id: 5, channel_id: 1, ext_id: 'M1', text: 'исходное сообщение', sender: { id: 2, name: 'Настя', handle: null } }),
    ];
    const wrapper = mount(MessageRow, { props: { message: msg({ channel_id: 1, reply_to_ext_id: 'M1' }) } });
    expect(wrapper.find('.reply-quote').text()).toBe('↪ Настя: исходное сообщение');
  });

  it('цель НЕ загружена — «ответ на сообщение»', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ channel_id: 1, reply_to_ext_id: 'UNKNOWN' }) },
    });
    expect(wrapper.find('.reply-quote').text()).toBe('↪ ответ на сообщение');
  });

  it('reply_to_ext_id=null — цитаты нет вовсе', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ reply_to_ext_id: null }) } });
    expect(wrapper.find('.reply-quote').exists()).toBe(false);
  });

  it('удалённое сообщение — цитата не показывается', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ deleted: true, text: null, reply_to_ext_id: 'M1' }) },
    });
    expect(wrapper.find('.reply-quote').exists()).toBe(false);
  });

  it('клик по цитате не падает, даже если DOM-узел цели не смонтирован', async () => {
    store.state.sync.messagesByChannel[1] = [msg({ id: 5, channel_id: 1, ext_id: 'M1', text: 'x' })];
    const wrapper = mount(MessageRow, { props: { message: msg({ channel_id: 1, reply_to_ext_id: 'M1' }) } });
    await expect(wrapper.find('.reply-quote').trigger('click')).resolves.toBeUndefined();
  });
});

describe('21 группа 4: цитата — рендерится только текстом (XSS-набор из 08-render-xss)', () => {
  const XSS_PAYLOADS = [
    '<img src=x onerror=alert(1)>',
    '<svg><script>alert(1)</script>',
    'javascript:alert(1)',
    '"><script>alert(1)</script>',
  ];

  it.each(XSS_PAYLOADS)('payload %s в имени/тексте цели — без HTML, только текст', (payload) => {
    store.state.sync.messagesByChannel[1] = [
      msg({ id: 5, channel_id: 1, ext_id: 'M1', text: payload, sender: { id: 2, name: payload, handle: null } }),
    ];
    const wrapper = mount(MessageRow, { props: { message: msg({ channel_id: 1, reply_to_ext_id: 'M1' }) } });
    const quote = wrapper.find('.reply-quote').element;
    expect(quote.querySelectorAll('img, svg, script, iframe, object, embed').length).toBe(0);
    for (const el of quote.querySelectorAll('*')) {
      for (const attr of Array.from(el.attributes)) {
        expect(attr.name.toLowerCase().startsWith('on')).toBe(false);
      }
    }
    expect(quote.textContent).toContain(payload);
  });
});
