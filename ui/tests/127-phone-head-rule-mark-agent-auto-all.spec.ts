// 15.10 (находки владельца): проект рядом с ролью в шапке окна сессии на телефоне; «правило #N» у строки «Нужна реакция»;
// «всем: Мне / Агенту» в таблице «Кто отвечает на карточки» («⋯ -> Автопилот»).
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { AutopilotState, HomeReactItem, HubSession, Project } from '../src/api/types';
import AutopilotPanel from '../src/components/AutopilotPanel.vue';
import PhoneSessions from '../src/components/PhoneSessions.vue';
import WaitList from '../src/components/WaitList.vue';
import { autopilotUi, resetAutopilot } from '../src/store/autopilot';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';
import { CARD_ROUTE_BULK_GROUPS, CARD_ROUTE_GROUPS } from '../src/util/owner_channel';

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}
let sent: Sent[] = [];

const PROJECT = { id: 3, name: 'Umbrella', color: '#f00', sort: 0, muted: false, workdir: '/p', unread: 0 } as Project;
const AUTOPILOT = {
  switches: [],
  global: {},
  projects: {},
  local: true,
  delegates: { '3': { head_channel: true, card_delegate: null, lines: [] } },
} as unknown as AutopilotState;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetAutopilot();
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      sent.push({ method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.pathname === '/api/v1/autopilot') return json(AUTOPILOT);
      if (url.pathname === '/api/v1/sessions') return json({ items: Object.values(store.state.sessions.items) });
      if (url.pathname === '/api/v1/subs') return json({ items: [], channels: [] });
      return json([]);
    }),
  );
  store.state.projects = [PROJECT];
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
});

afterEach(() => {
  store.state.phoneSession = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('шапка окна сессии на телефоне', () => {
  it('рядом с ролью — имя проекта', async () => {
    store.state.sessions.items = {
      5: { id: 5, project_id: 3, role: 'менеджер', role_no: 1, engine: 'claude', mode: 'manual', state: 'idle', closed_ts: null } as HubSession,
    };
    store.state.phoneSession = { kind: 'session', id: 5 };
    const w = mount(PhoneSessions);
    await flushPromises();
    const head = w.get('.phone-session-head');
    expect(head.text()).toContain('менеджер');
    // Ф90-1: шапка — одна строка; проект — в «Сведениях» меню «⋯»
    expect(head.find('.phone-session-project').exists()).toBe(false);
    await head.get('.phone-session-menu-button').trigger('click');
    expect(w.get('.session-menu-info').text()).toContain('Umbrella');
    w.unmount();
  });
});

describe('«правило #N» у строки «Нужна реакция»', () => {
  it('номер и название правила видны у строки на «Ждёт»', () => {
    const item = {
      channel: { id: 9, title: 'Заказ', project_id: 3, source: 'mail' },
      project: { id: 3, name: 'Umbrella', color: null },
      last: { sender: 'Площадка', excerpt: 'письмо', ts: 1000, message_id: 4 },
      card: null,
      draft_count: 0,
      rule_hits: [12],
      rules: [{ id: 12, title: 'Заказы FL' }],
    } as unknown as HomeReactItem;
    store.state.home = { ...store.state.home, react: [item], drafts: [] };
    const w = mount(WaitList);
    const mark = w.get('.rule-mark');
    expect(mark.text()).toBe('правило #12');
    expect(mark.attributes('title')).toContain('Заказы FL');
  });
});

describe('Таблица маршрута: «всем»', () => {
  it.each([
    ['agent', 'agent'],
    ['me', 'me'],
  ])('кнопка %s -> один PATCH со всеми группами', async (attr, value) => {
    autopilotUi.data = AUTOPILOT;
    autopilotUi.loaded = true;
    const w = mount(AutopilotPanel, { attachTo: document.body });
    await flushPromises();
    sent = [];
    await w.get(`[data-route-project="3"] [data-route-all="${attr}"]`).trigger('click');
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/3');
    expect(patches).toHaveLength(1);
    expect(patches[0]!.body).toEqual({ card_route: Object.fromEntries(CARD_ROUTE_BULK_GROUPS.map((g) => [g.key, value])) });
    // Ф93-1: массовое «всем» строку «Деньги» не трогает.
    expect(Object.keys((patches[0]!.body as { card_route: object }).card_route)).not.toContain('money');
    expect(CARD_ROUTE_GROUPS.some((g) => g.key === 'money')).toBe(true);
    w.unmount();
  });

  it('Ф93-1: подсветка «всем: Агенту» не смотрит на строку «Деньги» (там «Мне»)', async () => {
    const route = Object.fromEntries(CARD_ROUTE_BULK_GROUPS.map((g) => [g.key, 'agent']));
    store.state.projects = [{ ...PROJECT, card_route: { ...route, money: 'me' } } as unknown as Project];
    autopilotUi.data = AUTOPILOT;
    autopilotUi.loaded = true;
    const w = mount(AutopilotPanel, { attachTo: document.body });
    await flushPromises();
    expect(w.get('[data-route-project="3"] [data-route-all="agent"]').attributes('aria-pressed')).toBe('true');
    expect(w.get('[data-route-group="money"] [data-route="me"]').attributes('aria-pressed')).toBe('true');
    w.unmount();
  });
});
