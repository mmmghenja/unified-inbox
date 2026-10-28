// 24.10: «Отправить сейчас» у подтверждённой строки в очереди — по графику или к сроку. Строка
// на главной, окно черновика и сообщение в очереди в ленте беседы зовут `POST /outbox/{id}/send-now`; после
// успеха строка пропадает из «По графику», ошибка (409 с причиной) — текстом рядом.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OutboxListItem, Project } from '../src/api/types';
import DraftCard from '../src/components/DraftCard.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import { store } from '../src/store/instance';
import { buildBoard } from '../src/util/board';

const NOW = new Date(2026, 9, 24, 21, 0, 0).getTime();
const TOMORROW_1007 = new Date(2026, 9, 25, 10, 7, 0).getTime();

function project(): Project {
  return { id: 3, name: 'Umbrella', color: null, sort: 0, muted: false, workdir: null, unread: 0 };
}

function queued(id: number, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: 100 + id,
    project_id: 3,
    channel_title: `@lead_${id}`,
    text: `Касание ${id}`,
    status: 'queued',
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
    paced: true,
    not_before_ts: TOMORROW_1007,
    account_id: 1,
    ...overrides,
  };
}

interface Recorded {
  method: string;
  path: string;
}

function stubFetch(sendNow: () => Response): Recorded[] {
  const calls: Recorded[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      calls.push({ method: init?.method ?? 'GET', path: url.pathname });
      if (url.pathname.endsWith('/send-now')) return sendNow();
      if (url.pathname === '/api/v1/sync') return json({ changes: [], max_seq: 0 });
      return json([]);
    }),
  );
  return calls;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.draftSelection = [];
  store.state.projects = [project()];
  store.state.home = { react: [], drafts: [], scheduled: [], runs: [], sent: [], loadedAt: null, error: null };
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('«Отправить сейчас» у подтверждённой строки', () => {
  it('строка на главной: кнопка зовёт send-now, строка уходит из «По графику»', async () => {
    const calls = stubFetch(() => json({ status: 'queued' }));
    const item = queued(4);
    store.state.home.scheduled = [item];
    const board = buildBoard([project()], { react: [], drafts: [], scheduled: [item], runs: [], sent: [] }, NOW);
    const wrapper = mount(ProjectColumn, { props: { column: board.columns[0]! } });
    const row = wrapper.get('[data-outbox-id="4"]');
    expect(row.get('.draft-release-now').text()).toBe('Отправить сейчас');
    await row.get('.draft-release-now').trigger('click');
    await flushPromises();
    expect(calls.some((c) => c.path === '/api/v1/outbox/4/send-now' && c.method === 'POST')).toBe(true);
    expect(store.state.home.scheduled).toEqual([]);
    wrapper.unmount();
  });

  it('у черновика до подтверждения кнопки нет', () => {
    const wrapper = mount(DraftCard, { props: { item: queued(5, { status: 'pending_approval', not_before_ts: null }) } });
    expect(wrapper.find('.draft-release-now').exists()).toBe(false);
    wrapper.unmount();
  });

  it('окно черновика: отказ ядра — причина текстом, строка остаётся', async () => {
    stubFetch(() => json({ detail: 'уже уходит', error: 'not_waiting' }, 409));
    const item = queued(6, { paced: false, send_at_ts: TOMORROW_1007 });
    store.state.home.scheduled = [item];
    const wrapper = mount(DraftCard, { props: { item } });
    await wrapper.get('.draft-release-now').trigger('click');
    await flushPromises();
    expect(wrapper.get('.draft-card-error').text()).toContain('уже уходит');
    expect(store.state.home.scheduled).toEqual([item]);
    wrapper.unmount();
  });

  it('лента беседы: сообщение в очереди по графику — «Отправить сейчас» рядом с «Отменить»', async () => {
    const calls = stubFetch(() => json({ status: 'queued' }));
    const badge = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'queued', text: 'Касание', paced: true, notBeforeTs: TOMORROW_1007 },
    });
    const texts = badge.findAll('.outbox-actions button').map((b) => b.text());
    expect(texts).toEqual(['Отправить сейчас', 'Отменить']);
    await badge.get('.outbox-release-now').trigger('click');
    await flushPromises();
    expect(calls.some((c) => c.path === '/api/v1/outbox/9/send-now' && c.method === 'POST')).toBe(true);
    badge.unmount();

    const plain = mount(OutboxStatusBadge, { props: { outboxId: 10, status: 'queued', text: 'Ответ' } });
    expect(plain.find('.outbox-release-now').exists()).toBe(false); // обычная очередь не ждёт — кнопки нет
    plain.unmount();
  });
});
