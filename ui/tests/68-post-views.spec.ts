// Ф23-3 (`docs/architecture.md`): просмотры поста канала Telegram — «👁 1 234» под постом рядом с
// реакциями (разделитель тысяч — неразрывный пробел), подсказка «просмотры на ЧЧ:ММ · пересылок N»; нет `views` —
// ничего. Альбом поста — наибольший счётчик среди частей.
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import AlbumRow from '../src/components/AlbumRow.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { formatCount, parseViews, viewsLabel, viewsTitle } from '../src/render/views';
import { store } from '../src/store/instance';

const NBSP = ' ';

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
    text: 'пост канала',
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
    sender: { id: 1, name: 'Канал', handle: null },
    ...overrides,
  };
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
});

describe('Ф23-3: разбор и подписи', () => {
  it('тысячи — неразрывным пробелом', () => {
    expect(formatCount(7)).toBe('7');
    expect(formatCount(1234)).toBe(`1${NBSP}234`);
    expect(formatCount(1234567)).toBe(`1${NBSP}234${NBSP}567`);
    expect(viewsLabel({ views: 1234, forwards: null, ts: null })).toBe(`👁${NBSP}1${NBSP}234`);
  });

  it('подсказка: время снятия и пересылки; без них — короче', () => {
    const ts = new Date(2026, 9, 1, 15, 59).getTime();
    const hhmm = new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    expect(viewsTitle({ views: 10, forwards: 5, ts })).toBe(`просмотры на ${hhmm} · пересылок 5`);
    expect(viewsTitle({ views: 10, forwards: null, ts: null })).toBe('просмотры');
  });

  it('мусор — null; пересылки и время необязательны', () => {
    expect(parseViews(null)).toBeNull();
    expect(parseViews(undefined)).toBeNull();
    expect(parseViews({ views: '5' })).toBeNull();
    expect(parseViews({ views: -1 })).toBeNull();
    expect(parseViews({ views: 3, forwards: 'x', ts: 1.5 })).toEqual({ views: 3, forwards: null, ts: null });
  });
});

describe('Ф23-3: строка под постом', () => {
  it('пост канала со счётчиком — «👁 1 234» рядом с реакциями', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          views: { views: 1234, forwards: 5, ts: 1_700_000_000_000 },
          reactions_json: JSON.stringify([{ emoji: '👍', count: 3, me: false }]),
        }),
      },
    });
    const row = wrapper.find('.msg-counters');
    expect(row.exists()).toBe(true);
    expect(row.find('.reactions').exists()).toBe(true);
    const views = row.find('.post-views');
    expect(views.text()).toBe(`👁${NBSP}1${NBSP}234`);
    expect(views.attributes('title')).toContain('пересылок 5');
  });

  it('счётчик без реакций — одна строка «👁»', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ views: { views: 42, forwards: null, ts: null } }) },
    });
    expect(wrapper.find('.post-views').text()).toBe(`👁${NBSP}42`);
    expect(wrapper.find('.reactions').exists()).toBe(false);
  });

  it('нет views (не канал, старое ядро) — ничего, реакции как раньше', () => {
    const reactions = JSON.stringify([{ emoji: '👍', count: 1, me: false }]);
    for (const views of [null, undefined]) {
      const wrapper = mount(MessageRow, { props: { message: msg({ views, reactions_json: reactions }) } });
      expect(wrapper.find('.post-views').exists()).toBe(false);
      expect(wrapper.find('.msg-counters').exists()).toBe(false);
      expect(wrapper.find('.reactions').exists()).toBe(true);
    }
  });

  it('альбом поста — наибольший счётчик среди частей', () => {
    const wrapper = mount(AlbumRow, {
      props: {
        messages: [
          msg({ id: 1, grouped_id: 'g', views: { views: 900, forwards: 1, ts: null } }),
          msg({ id: 2, ext_id: '2', grouped_id: 'g', text: null, views: { views: 1500, forwards: 2, ts: null } }),
        ],
      },
    });
    expect(wrapper.find('.post-views').text()).toBe(`👁${NBSP}1${NBSP}500`);
  });

  it('альбом без счётчиков — строки нет', () => {
    const wrapper = mount(AlbumRow, {
      props: { messages: [msg({ id: 1, grouped_id: 'g' }), msg({ id: 2, ext_id: '2', grouped_id: 'g' })] },
    });
    expect(wrapper.find('.post-views').exists()).toBe(false);
  });
});
