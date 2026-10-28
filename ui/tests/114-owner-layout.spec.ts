// Ф56-3 (`docs/architecture.md`): где стоит окно «Агент». Старый столбец `orch` в сохранённой
// раскладке читается как `owner_channel`; пункт «⋯ → Агент» открывает столбец (компьютер) или вкладку (телефон); вкладка
// «Агент» телефона стоит после «Сессии» и не заменяет её; чата оркестратора в интерфейсе больше нет.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import BoardColumns from '../src/components/BoardColumns.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import TopMenu from '../src/components/TopMenu.vue';
import { boardState, normalizeLayout, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createOwnerState } from '../src/store/owner_channel';
import { initLayout } from '../src/util/layout';
import { fakeMedia } from './media';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  store.state.owner_channel = createOwnerState();
  store.state.phoneTab = 'wait';
  store.state.phoneSession = null;
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  vi.spyOn(window.history, 'pushState').mockImplementation(() => undefined);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/owner_channel/queue') {
        return json({ items: [{ ref: 'req:1', kind: 'permission', project_id: 3, project_name: 'П', session_id: 1, role: 'р', role_no: 1,
          path: { project: 'П', head: null, author: 'р' }, brief: 'x', full: {}, created_ts: 1, until_ts: null, in_channel: false,
          hold_ts: null, answers: [], agent_actions: [], errand: null }], seq: 1, agent: { online: false, seen_ts: null } });
      }
      return json([]);
    }),
  );
});

afterEach(() => {
  store.state.phoneTab = 'wait';
  store.state.phoneSession = null;
  initLayout(null);
  resetBoard();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('56-3 раскладка', () => {
  it('«orch» в сохранённой раскладке — столбец owner_channel; повтор схлопывается', () => {
    const layout = normalizeLayout({ open: [{ kind: 'orch' }, { kind: 'owner_channel' }, { kind: 'session', id: 4 }] });
    expect(layout.open).toEqual([{ kind: 'owner_channel' }, { kind: 'session', id: 4 }]);
  });

  it('столбец owner_channel рисует окно «Агент» (заголовок, очередь), чата оркестратора нет', async () => {
    boardState.layout = normalizeLayout({ open: [{ kind: 'orch' }] });
    const w = mount(BoardColumns, { attachTo: document.body });
    await flushPromises();
    expect(w.get('.board-col-name').text()).toBe('Агент');
    expect(w.find('section.agent-queue').exists()).toBe(true);
    expect(w.findAll('.owner-card')).toHaveLength(1);
    w.unmount();
  });

  it('пункт меню «Агент» (компьютер): открывает столбец, повтор не плодит; бейдж — число карточек', async () => {
    initLayout(fakeMedia(1440).matchMedia);
    await store.loadOwner();
    const w = mount(TopMenu, { attachTo: document.body });
    await w.get('.top-menu-button').trigger('click');
    const items = w.findAll('.top-menu-item');
    const labels = items.map((b) => b.text());
    const agentIdx = labels.findIndex((t) => t.startsWith('Агент'));
    const dispatchIdx = labels.findIndex((t) => t.startsWith('Агенты'));
    expect(agentIdx).toBeGreaterThanOrEqual(0);
    expect(agentIdx).toBeLessThan(dispatchIdx);
    expect(w.get('.agent-open .owner-badge').text()).toBe('1');
    await w.get('.agent-open').trigger('click');
    await w.get('.top-menu-button').trigger('click');
    await w.get('.agent-open').trigger('click');
    expect(boardState.layout.open.filter((o) => o.kind === 'owner_channel')).toHaveLength(1);
    w.unmount();
  });
});

describe('56-3 телефон', () => {
  beforeEach(() => {
    initLayout(fakeMedia(390).matchMedia);
  });

  it('пять вкладок; «Агент» стоит сразу после «Сессии», «Сессии» (ключ orch) остаётся', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    const tabs = w.findAll('nav.phone-tabs button');
    expect(tabs).toHaveLength(5);
    expect(tabs.map((t) => t.classes().find((c) => c.startsWith('phone-tab--')))).toEqual([
      'phone-tab--wait',
      'phone-tab--projects',
      'phone-tab--chats',
      'phone-tab--orch',
      'phone-tab--agent',
    ]);
    expect(w.get('.phone-tab--orch').text()).toBe('Сессии');
    expect(w.get('.phone-tab--agent').text()).toContain('Агент');
    w.unmount();
  });

  it('вкладка «Агент»: заголовок, очередь на весь экран, бейдж с числом карточек', async () => {
    await store.loadOwner();
    const w = mount(PhoneShell);
    await flushPromises();
    expect(w.get('.phone-tab--agent .owner-badge').text()).toBe('1');
    await w.get('.phone-tab--agent').trigger('click');
    await flushPromises();
    expect(store.state.phoneTab).toBe('agent');
    expect(w.get('.phone-title').text()).toBe('Агент');
    expect(w.find('section.agent-queue').exists()).toBe(true);
    expect(w.get('.phone-tab--agent').attributes('aria-current')).toBe('page');
    w.unmount();
  });

  it('select(agent) закрывает полноэкранную сессию «Сессий», как любая вкладка кроме «Сессии»', async () => {
    store.state.phoneTab = 'orch';
    store.state.phoneSession = { kind: 'session', id: 5 };
    const w = mount(PhoneShell);
    await flushPromises();
    store.setPhoneTab('orch');
    expect(store.state.phoneSession).not.toBeNull(); // «Сессии» уровень не закрывает
    await w.get('.phone-tab--agent').trigger('click');
    await flushPromises();
    expect(store.state.phoneSession).toBeNull();
    expect(store.state.phoneTab).toBe('agent');
    w.unmount();
  });

  it('пункт меню «Агент» на телефоне ведёт на вкладку', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    await w.get('.top-menu-button').trigger('click');
    await w.get('.agent-open').trigger('click');
    expect(store.state.phoneTab).toBe('agent');
    w.unmount();
  });
});
