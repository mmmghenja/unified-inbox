// Ф96-1: руки посреди поручения агента — «✋ Беру сам», «руки у тебя с ЧЧ:ММ», «▶ Продолжай» с пометкой.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { GroupOp, OwnerErrand, OwnerItem } from '../src/api/types';
import GroupOpCard from '../src/components/GroupOpCard.vue';
import OwnerCard from '../src/components/OwnerCard.vue';
import { store } from '../src/store/instance';
import { createOwnerState } from '../src/store/owner_channel';
import { errandLine } from '../src/util/owner_channel';

const NOW = 1760360000000;
const MIN = 60000;
const BASE: OwnerErrand = { id: 77, action: 'solve', label: 'Реши', text: null, created_ts: NOW - MIN, progress: 'проверяю', progress_ts: NOW - 10_000, auto: true };
const MINE: OwnerErrand = { ...BASE, hands: 'owner_channel', hands_by: 'agent', hands_ts: NOW - 2 * MIN, hands_why: 'нужен код из SMS' };

function item(errand: OwnerErrand): OwnerItem {
  return {
    ref: 'group_op:4', kind: 'group_op', project_id: 3, project_name: 'П', session_id: 1, role: 'маркетолог', role_no: 1,
    path: { project: 'П', head: 'оркестратор', author: 'маркетолог' }, brief: 'x', full: {}, created_ts: NOW, until_ts: null,
    in_channel: false, hold_ts: null, answers: [], agent_actions: [], errand,
  } as OwnerItem;
}

function groupOp(): GroupOp {
  return {
    id: 4, account_id: 11, project_id: 3, channel_id: null, kind: 'create', title: 'Клиенты', participants: [], reason: 'r',
    origin: 'session', status: 'pending', error: null, link: null, client_name: 'м', role: 'маркетолог', created_ts: NOW - MIN,
    decided_ts: null, done_ts: null, updated_ts: NOW - MIN, can_decline: true, seq: 1,
  } as GroupOp;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent { method: string; path: string; body: unknown }
let sent: Sent[] = [];
let handsStatus = 200;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  sent = [];
  handsStatus = 200;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      sent.push({ method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.pathname === '/api/v1/owner_channel/queue') return json({ items: [], seq: 1, agent: { online: true, seen_ts: NOW } });
      if (url.pathname.endsWith('/hands')) {
        return handsStatus === 200 ? json({ errand: BASE, changed: true }) : json({ detail: 'поручение уже закрыто' }, handsStatus);
      }
      return json({ ok: true });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф96-1: строка хода', () => {
  it('руки у агента — «решает агент», у владельца', () => {
    expect(errandLine(BASE, NOW)).toMatch(/^решает агент: /);
    expect(errandLine(MINE, NOW)).toMatch(/^ждёт тебя \(руки у тебя\): /);
    expect(errandLine(MINE, NOW)).not.toContain('решает агент');
  });
});

describe('Ф96-1: кнопки рук', () => {
  it('руки у агента: «Беру сам» -> POST hands to=owner_channel', async () => {
    const w = mount(OwnerCard, { props: { item: item(BASE), nowMs: NOW, agentOnline: true } });
    expect(w.find('[data-test="owner-hands-back"]').exists()).toBe(false);
    await w.get('[data-test="owner-hands-take"]').trigger('click');
    await flushPromises();
    const call = sent.find((s) => s.path === '/api/v1/owner_channel/errands/77/hands');
    expect(call?.method).toBe('POST');
    expect(call?.body).toEqual({ to: 'owner_channel' });
  });

  it('руки у владельца: время, «агент передал», «Продолжай» с note; «Беру сам» нет', async () => {
    const w = mount(OwnerCard, { props: { item: item(MINE), nowMs: NOW, agentOnline: true } });
    expect(w.find('[data-test="owner-hands-take"]').exists()).toBe(false);
    expect(w.get('[data-test="owner-hands-line"]').text()).toMatch(/^руки у тебя с \d{2}:\d{2}$/);
    expect(w.get('[data-test="owner-hands-why"]').text()).toBe('агент передал: нужен код из SMS');
    await w.get('[data-test="owner-hands-note"]').setValue('  ввёл код  ');
    await w.get('[data-test="owner-hands-back"]').trigger('click');
    await flushPromises();
    const call = sent.find((s) => s.path === '/api/v1/owner_channel/errands/77/hands');
    expect(call?.body).toEqual({ to: 'agent', note: 'ввёл код' });
  });

  it('«Продолжай» без пометки — тело без note; владелец забрал сам — без «агент передал»', async () => {
    const w = mount(OwnerCard, { props: { item: item({ ...MINE, hands_by: 'ui', hands_why: null }), nowMs: NOW, agentOnline: true } });
    expect(w.find('[data-test="owner-hands-why"]').exists()).toBe(false);
    await w.get('[data-test="owner-hands-back"]').trigger('click');
    await flushPromises();
    expect(sent.find((s) => s.path.endsWith('/hands'))?.body).toEqual({ to: 'agent' });
  });

  it('409 — строка ошибки', async () => {
    handsStatus = 409;
    const w = mount(OwnerCard, { props: { item: item(BASE), nowMs: NOW, agentOnline: true } });
    await w.get('[data-test="owner-hands-take"]').trigger('click');
    await flushPromises();
    expect(w.get('[data-test="owner-hands-error"]').text()).toContain('закрыто');
  });

  it('те же кнопки на карточке группы (ErrandBlock)', async () => {
    store.state.owner_channel.items = [item(MINE)];
    const w = mount(GroupOpCard, { props: { op: groupOp() } });
    expect(w.find('.errand-hands').exists()).toBe(true);
    expect(w.find('[data-test$="-hands-back"]').exists()).toBe(true);
  });
});
