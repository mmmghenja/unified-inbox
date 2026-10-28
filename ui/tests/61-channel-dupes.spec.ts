// Ф17-3: служебная строка «Пост канала «…»» с кнопкой «показать» и строка группы обсуждения в карточке заявки.
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { JoinRequest, Message } from '../src/api/types';
import JoinCard from '../src/components/JoinCard.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { parseForward } from '../src/render/forward';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1, seq: 1, first_seq: 1, channel_id: 1, project_id: 1, ext_id: '1', thread_ext_id: null,
    reply_to_ext_id: null, grouped_id: null, is_me: false, kind: 'event', text: 'Текст поста',
    entities_json: null, attachments_json: null, forward_json: null, mentions_me: false, refs_json: null,
    reactions_json: null, ts: 1000, edited_ts: null, deleted: false, outbox_id: null, outbox_status: null,
    sender: null, ...overrides,
  } as Message;
}

describe('61 Пост канала в группе обсуждения', () => {
  it('parseForward: auto только при auto === true', () => {
    expect(parseForward(JSON.stringify({ from_name: 'DEMO', auto: true }))?.auto).toBe(true);
    expect(parseForward(JSON.stringify({ from_name: 'DEMO' }))?.auto).toBe(false);
    expect(parseForward(JSON.stringify({ from_name: 'DEMO', auto: 'yes' }))?.auto).toBe(false);
  });

  it('служебная строка «Пост канала «DEMO»», текст раскрывается кнопкой «показать»', async () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ forward_json: JSON.stringify({ from_name: 'DEMO', channel_post: 7, auto: true }) }),
      },
    });
    expect(wrapper.classes()).toContain('message-row--event');
    expect(wrapper.text()).toContain('Пост канала «DEMO»');
    expect(wrapper.text()).not.toContain('Текст поста');
    await wrapper.get('.channel-post-toggle').trigger('click');
    expect(wrapper.text()).toContain('Текст поста');
    expect(wrapper.get('.channel-post-toggle').text()).toBe('скрыть');
  });

  it('служебное без auto — как раньше, без кнопки', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ text: 'участник вышел' }) } });
    expect(wrapper.find('.channel-post-toggle').exists()).toBe(false);
    expect(wrapper.text()).toContain('участник вышел');
  });
});

function join(linked: unknown): JoinRequest {
  return {
    id: 1, account_id: 1, project_id: 3, target_kind: 'username', target: 'x', reason: null, status: 'pending',
    auto: false,
    peek: {
      title: 'Канал', kind: 'broadcast', participants: 5, about: null, request_needed: false, member: false,
      forum: false, ext_id: '-100123', linked,
    },
    ext_id: '-100123', title: 'Канал', channel_id: null, error: null, client_name: null, created_ts: 1,
    decided_ts: null, done_ts: null, updated_ts: 1, can_decline: true, joining: false, seq: 1,
  } as unknown as JoinRequest;
}

describe('61 JoinCard: связанный чат', () => {
  it('группа обсуждения канала и канал группы, текстом (не HTML)', () => {
    const a = mount(JoinCard, {
      props: { join: join({ title: '<b>Чат</b>', username: 'demo_chat', kind: 'discussion' }) },
    });
    expect(a.get('.join-linked').text()).toBe('группа обсуждения: «<b>Чат</b>» (@demo_chat)');
    expect(a.find('.join-linked b').exists()).toBe(false);
    const b = mount(JoinCard, { props: { join: join({ title: 'DEMO', username: null, kind: 'channel' }) } });
    expect(b.get('.join-linked').text()).toBe('это группа обсуждения канала «DEMO»');
  });

  it('нет linked — строки нет', () => {
    expect(mount(JoinCard, { props: { join: join(null) } }).find('.join-linked').exists()).toBe(false);
  });
});
