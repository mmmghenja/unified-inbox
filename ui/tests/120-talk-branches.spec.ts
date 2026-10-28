// Ф66-2 C (`docs/architecture.md`): разговор с агентом по веткам сессий. Список веток
// («Общее» первой, непрочитанные выше), переключение (лента ветки), отправка в ветку, «обсудить с агентом» у
// карточки открывает ветку её сессии, «открыть сессию», прочитанность по ветке, телефон (список -> ветка -> «‹»).
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OwnerTalkBranch, OwnerTalkMsg } from '../src/api/types';
import WriteModalHost from '../src/components/WriteModalHost.vue';
import { boardState, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';
import { closeWrite, openWrite, writeModal } from '../src/store/writeModal';
import { createOwnerState } from '../src/store/owner_channel';
import { resetTalk, sortedBranches, talkState } from '../src/store/ownerTalk';
import { layout } from '../src/util/layout';

const NOW = new Date(2026, 9, 16, 12, 0, 0).getTime();

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

function msg(id: number, sessionId: number | null, text: string, overrides: Partial<OwnerTalkMsg> = {}): OwnerTalkMsg {
  return {
    id, ts: NOW, author: 'agent', text, ref: null, reply_to: null, handled_ts: null, read_ts: null, session_id: sessionId,
    ...overrides,
  };
}

function branch(sessionId: number | null, title: string, unread: number, lastTs: number): OwnerTalkBranch {
  return { session_id: sessionId, title, project_id: sessionId === null ? null : 3, role: null, last_id: 1, last_ts: lastTs, unread };
}

interface Sent {
  method: string;
  path: string;
  search: string;
  body: unknown;
}
let sent: Sent[] = [];
let branches: OwnerTalkBranch[] = [];
let all: OwnerTalkMsg[] = [];

function call(method: string, path: string): Sent[] {
  return sent.filter((s) => s.method === method && s.path === path);
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetTalk();
  sent = [];
  layout.mode = 'full';
  closeWrite();
  branches = [branch(null, 'Общее', 0, NOW - 5), branch(7, 'Umbrella · программист', 0, NOW - 100), branch(8, 'Umbrella · маркетолог', 2, NOW - 200)];
  all = [msg(1, null, 'общий ответ'), msg(2, 7, 'ответ про семь'), msg(3, 8, 'ответ про восемь')];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = {
        method: init?.method ?? 'GET',
        path: url.pathname,
        search: url.search,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      sent.push(s);
      const p = s.path;
      if (p === '/api/v1/owner_channel/talk/branches') return json({ items: branches });
      if (p === '/api/v1/owner_channel/talk' && s.method === 'GET') {
        const q = url.searchParams.get('session_id');
        const id = q === null ? undefined : q === 'general' ? null : Number(q);
        return json({ items: all.filter((m) => id === undefined || m.session_id === id), has_more: false });
      }
      if (p === '/api/v1/owner_channel/talk' && s.method === 'POST') {
        const body = s.body as { text: string; session_id?: number | null };
        const m = msg(100 + all.length, body.session_id ?? null, body.text, { author: 'owner_channel' });
        all = [...all, m];
        return json(m);
      }
      if (p === '/api/v1/owner_channel/talk/read') return json({ marked: 1 });
      if (p === '/api/v1/owner_channel/queue') return json({ items: [], seq: 1, agent: { online: true, seen_ts: NOW } });
      if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      return json([]);
    }),
  );
  store.state.projects = [];
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  boardState.offline = [];
  store.state.owner_channel = createOwnerState();
  store.state.owner_channel.agent.online = true;
});

afterEach(() => {
  closeWrite();
  resetTalk();
  layout.mode = 'full';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

async function mountHost() {
  const w = mount(WriteModalHost, { attachTo: document.body });
  await flushPromises();
  return w;
}

function feedTexts(): (string | undefined)[] {
  return [...document.body.querySelectorAll('.owner-talk .agent-msg-text')].map((e) => e.textContent?.trim());
}

function rows(): string[] {
  return [...document.body.querySelectorAll('.owner-branch')].map((e) => e.getAttribute('data-branch') ?? '');
}

describe('66-2 C ветки разговора', () => {
  it('порядок: «Общее» первой, затем непрочитанные, затем по времени', () => {
    const list = sortedBranches([branch(7, 'a', 0, 300), branch(null, 'Общее', 0, 1), branch(8, 'b', 1, 100), branch(9, 'c', 0, 500)]);
    expect(list.map((b) => b.session_id)).toEqual([null, 8, 9, 7]);
  });

  it('слева список веток с числом непрочитанного, справа лента «Общего»', async () => {
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    expect(rows()).toEqual(['general', '8', '7']);
    expect(document.body.querySelector('[data-branch="8"] [data-test="branch-unread"]')!.textContent).toBe('2');
    expect(feedTexts()).toEqual(['общий ответ']);
    expect(call('GET', '/api/v1/owner_channel/talk')[0]!.search).toContain('session_id=general');
    expect(document.body.querySelector('[data-test="branch-open-session"]')).toBeNull();
    host.unmount();
  });

  it('старое ядро без ручки веток: «Общее» остаётся', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        const url = new URL(input, 'http://localhost');
        if (url.pathname === '/api/v1/owner_channel/talk/branches') return new Response('{}', { status: 404, headers: { 'content-type': 'application/json' } });
        if (url.pathname === '/api/v1/owner_channel/talk') return json({ items: [], has_more: false });
        return json([]);
      }),
    );
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    expect(rows()).toEqual(['general']);
    host.unmount();
  });

  it('переключение: лента ветки, прочитанность по ветке, «открыть сессию»', async () => {
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    (document.body.querySelector('[data-branch="8"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(feedTexts()).toEqual(['ответ про восемь']);
    expect(document.body.querySelector('[data-test="branch-title"]')!.textContent).toBe('Umbrella · маркетолог');
    expect(call('POST', '/api/v1/owner_channel/talk/read').at(-1)!.body).toEqual({ upto: 3, session_id: 8 });
    expect(document.body.querySelector('[data-branch="8"] [data-test="branch-unread"]')).toBeNull();
    (document.body.querySelector('[data-test="branch-open-session"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(writeModal.target).toEqual({ kind: 'session', id: 8 });
    host.unmount();
  });

  it('отправка уходит в выбранную ветку', async () => {
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    (document.body.querySelector('[data-branch="7"]') as HTMLButtonElement).click();
    await flushPromises();
    const ta = document.body.querySelector('textarea[aria-label="Написать агенту"]') as HTMLTextAreaElement;
    ta.value = 'в семь';
    ta.dispatchEvent(new Event('input'));
    await flushPromises();
    (document.body.querySelector('.owner-talk form button[type="submit"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(call('POST', '/api/v1/owner_channel/talk')[0]!.body).toEqual({ text: 'в семь', session_id: 7 });
    expect(feedTexts()).toEqual(['ответ про семь', 'в семь']);
    host.unmount();
  });

  it('«Обсудить с агентом»: ветка сессии карточки с плашкой; без сессии — «Общее»', async () => {
    openWrite({ kind: 'owner_channel', branch: 7 }, { ref: 'req:1', title: 'Разрешить push' });
    let host = await mountHost();
    expect(talkState.branch).toBe(7);
    expect(feedTexts()).toEqual(['ответ про семь']);
    expect(document.body.querySelector('[data-test="talk-card"]')!.textContent).toContain('по карточке: Разрешить push');
    host.unmount();
    closeWrite();

    openWrite({ kind: 'owner_channel', branch: null }, { ref: 'req:2', title: 'Без сессии' });
    host = await mountHost();
    expect(talkState.branch).toBeNull();
    expect(feedTexts()).toEqual(['общий ответ']);
    expect(document.body.querySelector('[data-test="talk-card"]')).not.toBeNull();
    host.unmount();
  });

  it('телефон: открывается на «Общее» с полем ввода, «‹» — список веток, тап — ветка', async () => {
    layout.mode = 'phone';
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    // Ф80-1 F2: сразу «Общее» — поле ввода видно, список веток по «‹».
    expect(document.body.querySelector('.owner-talk-branches')).toBeNull();
    expect(document.body.querySelector('.owner-talk-main')).not.toBeNull();
    expect(document.body.querySelector('[data-test="branch-title"]')?.textContent).toBe('Общее');
    (document.body.querySelector('[data-test="branch-back"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.owner-talk-branches')).not.toBeNull();
    (document.body.querySelector('[data-branch="7"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.owner-talk-branches')).toBeNull();
    expect(feedTexts()).toEqual(['ответ про семь']);
    host.unmount();
  });

  it('телефон: «обсудить с агентом» сразу открывает ветку, а не список', async () => {
    layout.mode = 'phone';
    openWrite({ kind: 'owner_channel', branch: 8 }, { ref: 'req:1', title: 'X' });
    const host = await mountHost();
    expect(document.body.querySelector('.owner-talk-branches')).toBeNull();
    expect(feedTexts()).toEqual(['ответ про восемь']);
    host.unmount();
  });
});
