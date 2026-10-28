// 12.10.2026 (владелец: «в мобиле нет скрола, и не понимаю, что за 1 красный висит»): значок у вкладки «Сессии» —
// непрочитанные ответы агентов владельцу; на экране «Сессии» каждый такой ответ — строкой с именем агента, тап
// открывает диалог. Список сессий листается.
import { readFileSync } from 'node:fs';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { PeerMsg, Project, ProjectSub } from '../src/api/types';
import PhoneShell from '../src/components/PhoneShell.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';
import { initLayout } from '../src/util/layout';
import { fakeMedia } from './media';

const NOW = new Date(2026, 9, 12, 11, 0, 0).getTime();

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

const SUB = {
  id: 40,
  project_id: 3,
  role: 'маркетолог',
  role_no: 1,
  mode: 'messages',
  engine: 'claude',
  session_name: null,
  live: true,
  listening: true,
  waiting: false,
} as ProjectSub;

function reply(overrides: Partial<PeerMsg> = {}): PeerMsg {
  return {
    id: 7,
    from_user: false,
    to_user: true,
    from: { project_id: 3, role: 'маркетолог', role_no: 1, sub_id: 40 },
    to: null,
    text: 'готово',
    reply_to: null,
    chain_id: null,
    hop: 0,
    status: 'pending',
    reason: null,
    created_ts: NOW - 60_000,
    signaled_ts: null,
    signals: 0,
    read_ts: null,
    closed_ts: null,
    expires_ts: null,
    seq: 1,
    ...overrides,
  } as PeerMsg;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  initLayout(fakeMedia(390).matchMedia);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname === '/api/v1/subs' && url.searchParams.get('offline') === '1') return json({ lines: [] });
      if (url.pathname === '/api/v1/subs') return json({ items: [SUB], channels: [] });
      if (url.pathname === '/api/v1/sessions') return json({ items: [] });
      if (url.pathname === '/api/v1/peer-msgs') return json({ items: Object.values(store.state.peerMsgs.items) });
      return json([]);
    }),
  );
  vi.spyOn(window.history, 'pushState').mockImplementation(() => undefined);
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [
    { id: 3, name: 'Umbrella', color: null, sort: 0, muted: false, workdir: '/p', unread: 0, subs: true } as Project,
  ];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
  store.state.peerMsgs.agentSubs = [SUB];
  store.state.peerMsgs.items = { 7: reply() };
  store.state.phoneTab = 'orch';
  store.state.phoneSession = null;
});

afterEach(() => {
  store.state.phoneTab = 'wait';
  store.state.phoneSession = null;
  initLayout(null);
  resetBoard();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('телефон, «Сессии»: непрочитанный ответ агента назван строкой', () => {
  it('значок вкладки и строка «Ответ тебе: маркетолог · Umbrella»; тап открывает диалог', async () => {
    const w = mount(PhoneShell);
    await flushPromises();
    expect(w.get('.agent-replies-badge').text()).toBe('1');
    const row = w.get('.phone-sessions-reply');
    expect(row.text()).toContain('Ответ тебе: маркетолог');
    expect(row.text()).toContain('Umbrella');
    expect(row.get('.badge').text()).toBe('1');
    await row.trigger('click');
    expect(store.state.phoneSession).toMatchObject({ kind: 'agent' });
    w.unmount();
  });

  it('ответ агента, которого нет в списке, — строкой «в переписке»', async () => {
    store.state.peerMsgs.items = { 8: reply({ id: 8, from: { project_id: 0, role: null, role_no: null, sub_id: 99 } }) };
    const w = mount(PhoneShell);
    await flushPromises();
    expect(w.get('[data-reply-key="other"]').text()).toContain('1');
    w.unmount();
  });

  it('список сессий листается', () => {
    const css = readFileSync('src/styles/board-view.css', 'utf8');
    expect(css).toMatch(/\.phone-sessions \{\s*overflow-y: auto;/);
  });
});
