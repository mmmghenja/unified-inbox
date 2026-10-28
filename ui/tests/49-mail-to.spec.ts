// Ф11-8 (docs/architecture.md): у своего письма строка «кому: …» тем же
// приглушённым стилем, что «переслано от»; больше 3 адресов — «и ещё N», полный список в `title`; битый JSON —
// строки нет; только текстом.
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { parseMailTo } from '../src/render/mailTo';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
    project_id: 1,
    ext_id: '<m1@x>',
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: true,
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
    sender: null,
    ...overrides,
  };
}

describe('49 parseMailTo', () => {
  it('до трёх адресов — все через запятую', () => {
    expect(parseMailTo(JSON.stringify(['a@x.ru', 'b@y.ru']))).toEqual({
      label: 'a@x.ru, b@y.ru',
      title: 'a@x.ru, b@y.ru',
    });
  });

  it('больше трёх — «и ещё N», полный список в title', () => {
    const all = ['a@x.ru', 'b@x.ru', 'c@x.ru', 'd@x.ru', 'e@x.ru'];
    expect(parseMailTo(JSON.stringify(all))).toEqual({
      label: 'a@x.ru, b@x.ru, c@x.ru и ещё 2',
      title: all.join(', '),
    });
  });

  it('ровно три — без «и ещё»', () => {
    expect(parseMailTo(JSON.stringify(['a@x.ru', 'b@x.ru', 'c@x.ru']))?.label).toBe('a@x.ru, b@x.ru, c@x.ru');
  });

  it('пусто, битый JSON, не список, одни не-строки — null', () => {
    expect(parseMailTo(null)).toBeNull();
    expect(parseMailTo(undefined)).toBeNull();
    expect(parseMailTo('')).toBeNull();
    expect(parseMailTo('{не json')).toBeNull();
    expect(parseMailTo(JSON.stringify({ a: 1 }))).toBeNull();
    expect(parseMailTo(JSON.stringify([]))).toBeNull();
    expect(parseMailTo(JSON.stringify([1, null, '']))).toBeNull();
  });
});

describe('49 MessageRow: строка «кому»', () => {
  it('есть — над текстом, стилем «переслано от», title со всем списком', () => {
    const all = ['a@x.ru', 'b@x.ru', 'c@x.ru', 'd@x.ru'];
    const wrapper = mount(MessageRow, { props: { message: msg({ mail_to_json: JSON.stringify(all) }) } });
    const line = wrapper.find('.mail-to');
    expect(line.exists()).toBe(true);
    expect(line.classes()).toContain('forwarded');
    expect(line.text()).toBe('кому: a@x.ru, b@x.ru, c@x.ru и ещё 1');
    expect(line.attributes('title')).toBe(all.join(', '));
    const html = wrapper.html();
    expect(html.indexOf('кому:')).toBeLessThan(html.indexOf('Добрый день'));
  });

  it('нет поля или null — строки нет', () => {
    expect(mount(MessageRow, { props: { message: msg({}) } }).find('.mail-to').exists()).toBe(false);
    expect(mount(MessageRow, { props: { message: msg({ mail_to_json: null }) } }).find('.mail-to').exists()).toBe(
      false,
    );
  });

  it('битый JSON — строки нет, без падения', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ mail_to_json: '["a@x.ru"' }) } });
    expect(wrapper.find('.mail-to').exists()).toBe(false);
    expect(wrapper.text()).toContain('Добрый день');
  });

  it('только текстом: разметка в адресе не становится HTML', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ mail_to_json: JSON.stringify(['<img src=x onerror=alert(1)>@x.ru']) }) },
    });
    expect(wrapper.find('.mail-to img').exists()).toBe(false);
    expect(wrapper.find('.mail-to').text()).toContain('<img src=x onerror=alert(1)>@x.ru');
  });
});
