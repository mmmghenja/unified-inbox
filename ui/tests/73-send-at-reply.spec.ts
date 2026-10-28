// Ф30-2 (`docs/architecture.md`, тест B7): догон к сроку с
// `cancel_if_reply` — к пометке времени «· не уйдёт, если ответят» (черновик и подтверждённый, карточка, строка,
// бейдж ленты); без флажка — без добавки; неудача — причина ядра как есть.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OutboxListItem } from '../src/api/types';
import DraftCard from '../src/components/DraftCard.vue';
import DraftRow from '../src/components/DraftRow.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import { cancelIfReplyMark, sendAtLabel, slotLabel } from '../src/util/pace';

const NOW = new Date(2026, 9, 2, 9, 0, 0).getTime();
const AT = new Date(2026, 9, 5, 11, 0, 0).getTime();
const MARK = ' · не уйдёт, если ответят';
const REPLIED = 'после подтверждения в беседе был ответ — догон не ушёл';

function draft(id: number, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: 100 + id,
    project_id: 3,
    channel_title: 'Партнёр',
    text: `Догон ${id}`,
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: NOW - 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Umbrella',
    source: 'telegram',
    kind: 'dm',
    author: 'mcp:iip',
    reply_to: null,
    thread: null,
    paced: false,
    not_before_ts: null,
    send_at_ts: AT,
    cancel_if_reply: true,
    account_id: 1,
    ...overrides,
  };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Ф30-2: пометка «не уйдёт, если ответят»', () => {
  it('cancelIfReplyMark — только при send_at и флажке', () => {
    expect(cancelIfReplyMark(AT, true)).toBe(MARK);
    expect(cancelIfReplyMark(AT, false)).toBe('');
    expect(cancelIfReplyMark(AT, undefined)).toBe('');
    expect(cancelIfReplyMark(null, true)).toBe('');
    expect(cancelIfReplyMark(undefined, true)).toBe('');
  });

  it('карточка и строка: черновик — «к …» с добавкой; подтверждённый — «уйдёт …» с добавкой', () => {
    for (const component of [DraftCard, DraftRow]) {
      const pending = mount(component, { props: { item: draft(1) } });
      expect(pending.get('.draft-card-send-at').text()).toBe(`${sendAtLabel(AT)}${MARK}`);
      pending.unmount();
      const queued = mount(component, { props: { item: draft(2, { status: 'queued', not_before_ts: AT }) } });
      expect(queued.get('.draft-card-slot').text()).toBe(`${slotLabel(AT)}${MARK}`);
      queued.unmount();
      const plain = mount(component, { props: { item: draft(3, { cancel_if_reply: false }) } });
      expect(plain.get('.draft-card-send-at').text()).toBe(sendAtLabel(AT));
      plain.unmount();
    }
  });

  it('бейдж ленты: черновик и подтверждённый — с добавкой; без флажка — как в Ф30-1', () => {
    const pending = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'pending_approval', text: 'Догон', sendAtTs: AT, cancelIfReply: true },
    });
    expect(pending.get('.outbox-label').text()).toBe(
      `черновик #9 · ждёт подтверждения · ${sendAtLabel(AT)}${MARK}`,
    );
    pending.unmount();
    const queued = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'queued', text: 'Догон', notBeforeTs: AT, sendAtTs: AT, cancelIfReply: true },
    });
    expect(queued.get('.outbox-label').text().replace(/^#\d+ · /, '')).toBe(`${slotLabel(AT)}${MARK}`);
    queued.unmount();
    const plain = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'queued', text: 'Пост', notBeforeTs: AT, sendAtTs: AT },
    });
    expect(plain.get('.outbox-label').text().replace(/^#\d+ · /, '')).toBe(slotLabel(AT));
    plain.unmount();
  });

  it('бейдж: failed «ответили» — причина ядра как есть', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ error: REPLIED }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })),
    );
    const wrapper = mount(OutboxStatusBadge, {
      props: { outboxId: 15, status: 'failed', text: 'Догон', sendAtTs: AT, cancelIfReply: true },
    });
    await flushPromises();
    expect(wrapper.get('.outbox-label--error').text().replace(/^#\d+ · /, '')).toBe(`не отправлено — ${REPLIED}`);
    wrapper.unmount();
  });
});
