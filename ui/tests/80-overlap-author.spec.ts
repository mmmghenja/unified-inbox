// Ф39 C (`docs/architecture.md`): несколько слушающих сессий у беседы и роль
// сессии-автора у черновика и исходящего сообщения.
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { ChanSub, Message, OutboxListItem } from '../src/api/types';
import DraftCard from '../src/components/DraftCard.vue';
import DraftRow from '../src/components/DraftRow.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { listenerName, listeningLabelOf } from '../src/util/listeners';

function sub(id: number, overrides: Partial<ChanSub> = {}): ChanSub {
  return {
    id,
    channel_id: 5,
    project_id: 3,
    session_name: null,
    session_pid: 1000 + id,
    live: true,
    listening: true,
    waiting: true,
    ended_ts: null,
    role: null,
    role_no: null,
    ...overrides,
  };
}

describe('Ф39 C: подпись слушающих', () => {
  it('одна роль — «слушает: роль»', () => {
    expect(listeningLabelOf([sub(1, { role: 'маркетолог', role_no: 1 })], 5)).toBe('слушает: маркетолог');
  });

  it('две подписки — «слушают: …» через запятую', () => {
    const subs = [sub(1, { role: 'маркетолог', role_no: 1 }), sub(2, { role: 'сисадмин', role_no: 1 })];
    expect(listeningLabelOf(subs, 5)).toBe('слушают: маркетолог, сисадмин');
  });

  it('роль с номером больше 1 — «маркетолог 2»', () => {
    expect(listenerName(sub(1, { role: 'маркетолог', role_no: 2 }))).toBe('маркетолог 2');
    expect(listenerName(sub(1, { role: 'маркетолог', role_no: 1 }))).toBe('маркетолог');
  });

  it('без роли — имя сессии, без имени — #pid', () => {
    expect(listenerName(sub(1, { session_name: 'hub-9' }))).toBe('hub-9');
    expect(listenerName(sub(7))).toBe('#1007');
  });

  it('завершённые, неживые и чужие беседы не считаются', () => {
    const subs = [
      sub(1, { role: 'a', ended_ts: 5 }),
      sub(2, { role: 'b', live: false }),
      sub(3, { role: 'c', channel_id: 6 }),
    ];
    expect(listeningLabelOf(subs, 5)).toBeNull();
    expect(listeningLabelOf([...subs, sub(4, { role: 'd' })], 5)).toBe('слушает: d');
  });
});

function draft(overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id: 1,
    channel_id: 5,
    project_id: 3,
    channel_title: 'Беседа',
    text: 'текст',
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Umbrella',
    source: 'telegram',
    kind: 'dm',
    author: 'mcp:iip',
    reply_to: null,
    thread: null,
    ...overrides,
  };
}

describe('Ф39 C: роль автора у черновика', () => {
  it('есть author_role — метка с ролью и подсказкой', () => {
    const w = mount(DraftCard, { props: { item: draft({ author_role: 'сисадмин' }) } });
    const mark = w.get('.draft-card-role');
    expect(mark.text()).toBe('сисадмин');
    expect(mark.attributes('title')).toBe('черновик положила сессия с этой ролью');
  });

  // Приёмка: строка черновика на главной — свой компонент (`DraftRow`), не `DraftCard`.
  it('строка на главной: есть author_role — метка, нет — метки нет', () => {
    const w = mount(DraftRow, { props: { item: draft({ author_role: 'маркетолог 2' }), selectable: true } });
    const mark = w.find('.draft-row .draft-card-role');
    expect(mark.exists()).toBe(true);
    expect(mark.text()).toBe('маркетолог 2');
    expect(mount(DraftRow, { props: { item: draft(), selectable: true } }).find('.draft-card-role').exists()).toBe(
      false,
    );
  });

  it('нет поля или null — метки нет', () => {
    expect(mount(DraftCard, { props: { item: draft() } }).find('.draft-card-role').exists()).toBe(false);
    expect(
      mount(DraftCard, { props: { item: draft({ author_role: null }) } }).find('.draft-card-role').exists(),
    ).toBe(false);
  });
});

function msg(overrides: Partial<Message> = {}): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 3,
    ext_id: '1',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: true,
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
    sender: { id: 1, name: 'я', handle: null },
    ...overrides,
  };
}

describe('Ф39 C: метка роли у своего сообщения', () => {
  it('с автором — «роль: …»', () => {
    const w = mount(MessageRow, { props: { message: msg({ outbox_author_role: 'сисадмин' }) } });
    expect(w.get('.auto-role-mark').text()).toBe('роль: сисадмин');
  });

  it('ушло без подтверждения — только «агент: …»', () => {
    const w = mount(MessageRow, {
      props: { message: msg({ outbox_auto_role: 'маркетолог', outbox_author_role: 'маркетолог' }) },
    });
    const marks = w.findAll('.auto-role-mark');
    expect(marks).toHaveLength(1);
    expect(marks[0].text()).toBe('агент: маркетолог');
  });

  it('без полей — меток нет', () => {
    expect(mount(MessageRow, { props: { message: msg() } }).find('.auto-role-mark').exists()).toBe(false);
  });
});
