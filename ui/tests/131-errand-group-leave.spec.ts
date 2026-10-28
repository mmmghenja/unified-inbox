// Ф66-3 (A): «у агента» на карточках групп WhatsApp (`GroupOpCard`) и «выйти» (`LeaveCard`): строка хода,
// «Остановить агента», блок возврата с «Пусть решает»; кнопки самой карточки остаются на месте.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { GroupOp, LeaveRequest, OwnerItem } from '../src/api/types';
import GroupOpCard from '../src/components/GroupOpCard.vue';
import LeaveCard from '../src/components/LeaveCard.vue';
import { store } from '../src/store/instance';
import { createOwnerState } from '../src/store/owner_channel';

const NOW = 1760360000000;
const MIN = 60000;
const ERRAND = { id: 77, action: 'solve', label: 'Реши', text: null, created_ts: NOW - MIN, progress: 'проверяю', progress_ts: NOW - 10_000, auto: true };

function groupOp(): GroupOp {
  return {
    id: 4, account_id: 11, project_id: 3, channel_id: null, kind: 'create', title: 'Клиенты',
    participants: [{ i: 0, kind: 'chat', mask: '', name: 'Петя', channel_id: 9 }],
    reason: 'нужна группа', origin: 'session', status: 'pending', error: null, link: null, client_name: 'маркетолог',
    role: 'маркетолог', created_ts: NOW - MIN, decided_ts: null, done_ts: null, updated_ts: NOW - MIN, can_decline: true, seq: 1,
  } as GroupOp;
}

function leave(): LeaveRequest {
  return {
    id: 2, account_id: 1, project_id: 3, channel_id: 5, title: 'Канал', chat_kind: 'channel', reason: 'не нужен', origin: 'session',
    status: 'pending', result: null, error: null, client_name: 'программист', created_ts: NOW - MIN, decided_ts: null,
    done_ts: null, returned_ts: null, updated_ts: NOW - MIN, can_decline: true,
  } as LeaveRequest;
}

function queueItem(ref: string, over: Partial<OwnerItem>): OwnerItem {
  return {
    ref, kind: 'group_op', project_id: 3, project_name: 'П', session_id: 1, role: 'маркетолог', role_no: 1,
    path: { project: 'П', head: 'оркестратор', author: 'маркетолог' }, brief: 'x', full: {}, created_ts: NOW, until_ts: null,
    in_channel: false, hold_ts: null, answers: [], agent_actions: [], errand: null, ...over,
  } as OwnerItem;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}
let sent: Sent[] = [];

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      sent.push({ method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.pathname === '/api/v1/owner_channel/queue') return json({ items: [], seq: 1, agent: { online: true, seen_ts: NOW } });
      return json({ ok: true });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф66-3 A: поручение агента на карточках групп и «выйти»', () => {
  it('нет поручения — блока нет', () => {
    const w = mount(GroupOpCard, { props: { op: groupOp() } });
    expect(w.find('.errand-block').exists()).toBe(false);
  });

  it('группа WhatsApp: строка хода, «Остановить агента» -> DELETE, кнопки владельца на месте', async () => {
    store.state.owner_channel.items = [queueItem('group_op:4', { errand: ERRAND })];
    const w = mount(GroupOpCard, { props: { op: groupOp() } });
    expect(w.get('[data-test="group-op-errand"]').text()).toContain('решает агент: Реши · проверяю');
    expect(w.get('.group-op-approve').text()).toBe('Да');
    await w.get('[data-test="group-op-take-back"]').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'DELETE' && s.path === '/api/v1/owner_channel/errands/77')).toBe(true);
  });

  it('«выйти»: поручение другой карточки не показывается; своё — показывается', () => {
    store.state.owner_channel.items = [queueItem('leave:9', { kind: 'leave', errand: ERRAND })];
    expect(mount(LeaveCard, { props: { leave: leave() } }).find('.errand-block').exists()).toBe(false);
    store.state.owner_channel.items = [queueItem('leave:2', { kind: 'leave', errand: ERRAND })];
    const w = mount(LeaveCard, { props: { leave: leave() } });
    expect(w.get('[data-test="leave-errand"]').text()).toContain('решает агент');
    expect(w.get('.leave-approve').text()).toBe('Выйти');
  });

  it('агент вернул: блок возврата, «Пусть решает» с текстом -> POST /owner_channel/group_op:4/errand', async () => {
    store.state.owner_channel.items = [
      queueItem('group_op:4', { errand_returned: { reason: 'нет номера', ts: NOW, by: 'agent', detail: 'ждал ответа' } }),
    ];
    const w = mount(GroupOpCard, { props: { op: groupOp() } });
    expect(w.get('[data-test="group-op-returned"]').text()).toContain('Агент вернул тебе: нет номера');
    await w.get('[data-test="group-op-say"]').setValue('попробуй ещё раз');
    await w.get('[data-test="group-op-let-decide"]').trigger('click');
    await flushPromises();
    const post = sent.find((s) => s.method === 'POST' && decodeURIComponent(s.path).endsWith('/errand'))!;
    expect(decodeURIComponent(post.path)).toBe('/api/v1/owner_channel/group_op:4/errand');
    expect(post.body).toEqual({ text: 'попробуй ещё раз' });
  });
});
