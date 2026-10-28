// Спека 1d-1: п.3 (пересланное), п.4 (реакции), п.6 (служебные kind='event').
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { parseForward } from '../src/render/forward';
import { parseReactions } from '../src/render/reactions';

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

describe('18 п.3 Пересланное: «переслано от <from_name | from_ext_id>»', () => {
  it('from_name в приоритете над from_ext_id', () => {
    const parsed = parseForward(JSON.stringify({ from_name: 'Иван', from_ext_id: '999', ts: 1 }));
    expect(parsed?.label).toBe('Иван');
  });

  it('без from_name — берётся from_ext_id', () => {
    const parsed = parseForward(JSON.stringify({ from_ext_id: '999', ts: 1 }));
    expect(parsed?.label).toBe('999');
  });

  it('ни того ни другого / битый JSON / не объект -> null, строка не показывается', () => {
    expect(parseForward(JSON.stringify({ ts: 1 }))).toBeNull();
    expect(parseForward('не json')).toBeNull();
    expect(parseForward('[1,2,3]')).toBeNull();
    expect(parseForward(null)).toBeNull();
  });

  it('MessageRow: строка «переслано от …» стоит НАД текстом', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ text: 'исходный текст', forward_json: JSON.stringify({ from_name: 'Отправитель' }) }),
      },
    });
    expect(wrapper.find('.forwarded').text()).toBe('переслано от Отправитель');
    const html = wrapper.html();
    expect(html.indexOf('переслано от')).toBeLessThan(html.indexOf('исходный текст'));
  });

  // Ф8-1 C2 (решение архитектора 24.09): удалённое остаётся видно — пересылка показывается и у удалённого.
  it('MessageRow: удалённое сообщение — строка пересылки остаётся видна', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ deleted: true, text: null, forward_json: JSON.stringify({ from_name: 'X' }) }),
      },
    });
    expect(wrapper.find('.forwarded').text()).toContain('X');
  });
});

describe('18 п.4 Реакции: «👍 3 ❤️ 1», своя выделена, custom -> ◆', () => {
  it('обычные эмодзи-реакции разбираются по порядку', () => {
    const parsed = parseReactions(JSON.stringify([{ emoji: '👍', count: 3 }, { emoji: '❤️', count: 1, me: true }]));
    expect(parsed).toEqual([
      { label: '👍', count: 3, me: false, raw: '👍', custom: false },
      { label: '❤️', count: 1, me: true, raw: '❤️', custom: false },
    ]);
  });

  it('custom -> «◆»', () => {
    const parsed = parseReactions(JSON.stringify([{ custom: 'abc123', count: 2 }]));
    expect(parsed).toEqual([{ label: '◆', count: 2, me: false, raw: 'abc123', custom: true }]);
  });

  it('невалидные элементы (без count, count<=0, без emoji/custom) отбрасываются', () => {
    const parsed = parseReactions(
      JSON.stringify([{ emoji: '👍' }, { emoji: '🔥', count: 0 }, { count: 5 }, { emoji: '✅', count: 5 }]),
    );
    expect(parsed).toEqual([{ label: '✅', count: 5, me: false, raw: '✅', custom: false }]);
  });

  it('MessageRow: реакции под сообщением, своя выделена классом mine', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ reactions_json: JSON.stringify([{ emoji: '👍', count: 3, me: true }, { emoji: '❤️', count: 1 }]) }),
      },
    });
    const reactionEls = wrapper.findAll('.reaction');
    expect(reactionEls).toHaveLength(2);
    expect(reactionEls[0]!.text()).toBe('👍 3');
    expect(reactionEls[0]!.classes()).toContain('mine');
    expect(reactionEls[1]!.classes()).not.toContain('mine');
  });
});

describe('18 п.6 Служебные (kind=event): серой строкой по центру, без отправителя', () => {
  it('MessageRow: нет имени отправителя/времени, есть текст события', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ kind: 'event', text: 'участник вышел из чата', sender: { id: 1, name: 'Кто-то', handle: null } }),
      },
    });
    expect(wrapper.find('.sender-name').exists()).toBe(false);
    expect(wrapper.find('.message-ts').exists()).toBe(false);
    expect(wrapper.classes()).toContain('message-row--event');
    expect(wrapper.text()).toContain('участник вышел из чата');
  });
});
