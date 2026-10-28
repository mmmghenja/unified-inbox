// Ф58-1 B (`docs/architecture.md`): окно «Написать…». Сборка списка адресатов (порядок, группы,
// вне проектов, «не в сети», поиск), открытие из меню, окна «Агент» и карточки очереди («обсудить с агентом»),
// разговор с агентом владельца (отправка, отметка прочитанного, «не на связи», «ждёт агента»), сессия хаба (реплика
// уходит `say`) и внешний агент (`peer-msgs`), закрытие, телефонный режим (список -> разговор -> «‹»).
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, OfflineLine, Project, ProjectSub, OwnerQueue, OwnerTalkMsg } from '../src/api/types';
import AgentQueue from '../src/components/AgentQueue.vue';
import TopBar from '../src/components/TopBar.vue';
import TopMenu from '../src/components/TopMenu.vue';
import WriteModalHost from '../src/components/WriteModalHost.vue';
import { boardState, resetBoard } from '../src/store/board';
import { buildBoardList } from '../src/util/boardList';
import { store } from '../src/store/instance';
import { buildAgentLines, createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';
import { closeWrite, openWrite, writeModal } from '../src/store/writeModal';
import { createOwnerState } from '../src/store/owner_channel';
import { resetTalk } from '../src/store/ownerTalk';
import { layout } from '../src/util/layout';
import { buildWriteTargets, filterWriteTargets, firstWriteTarget } from '../src/util/writeTargets';

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const NOW = new Date(2026, 9, 14, 12, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function project(id: number, name: string): Project {
  return { id, name, color: '#ff0000', sort: id, muted: false, workdir: `/p${id}`, unread: 0, subs: true } as Project;
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: null, effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null,
    pending_requests: 0, queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null,
    ...overrides,
  } as HubSession;
}

function sub(overrides: Partial<ProjectSub> = {}): ProjectSub {
  return {
    id: 40, project_id: 3, role: 'маркетолог', role_no: 1, mode: 'messages', engine: 'claude', session_name: null,
    live: true, listening: true, waiting: false, ...overrides,
  } as ProjectSub;
}

function offline(overrides: Partial<OfflineLine> = {}): OfflineLine {
  return {
    key: 'r:3:аналитик:1', project_id: 3, role: 'аналитик', role_no: 1, engine: 'claude', session_name: null, cwd: null,
    ended_ts: NOW - 1000, end_reason: null, pending: 0, sub_id: 77,
    ...overrides,
  } as OfflineLine;
}

function talkMsg(overrides: Partial<OwnerTalkMsg>): OwnerTalkMsg {
  return { id: 1, ts: NOW, author: 'agent', text: 'привет', ref: null, reply_to: null, handled_ts: null, read_ts: null, ...overrides };
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}
let sent: Sent[] = [];
let talk: OwnerTalkMsg[] = [];
let queue: OwnerQueue;

function posts(suffix: string): Sent[] {
  return sent.filter((s) => s.method === 'POST' && s.path.endsWith(suffix));
}

function setupStore(): void {
  store.state.projects = [project(3, 'Umbrella'), project(4, 'Личное')];
  store.state.sessions = createSessionsState();
  store.state.sessions.items[5] = session();
  store.state.peerMsgs = createPeerMsgState();
  store.state.peerMsgs.agentSubs = [
    sub(),
    sub({ id: 41, project_id: 0, role: 'сисадмин', engine: 'opencode', listening: false }),
  ];
  boardState.offline = [offline()];
  boardState.offlineGone = false;
  store.state.owner_channel = createOwnerState();
  store.state.owner_channel.agent.online = true;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetTalk();
  sent = [];
  talk = [];
  queue = JSON.parse(JSON.stringify(FIXTURE)) as OwnerQueue;
  layout.mode = 'full';
  closeWrite();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const raw = init?.body;
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname, body: typeof raw === 'string' ? JSON.parse(raw) : raw };
      sent.push(s);
      const p = s.path;
      if (p === '/api/v1/owner_channel/talk' && s.method === 'GET') return json({ items: talk, has_more: false });
      if (p === '/api/v1/owner_channel/talk' && s.method === 'POST') {
        const body = s.body as { text: string; ref?: string; files?: string[] };
        const files = (body.files ?? []).map((id) => ({ id, name: 'экран.png', size: 4, mime: 'image/png', kind: 'image' as const, gone: false }));
        const msg = talkMsg({ id: 100 + talk.length, author: 'owner_channel', text: body.text, ref: body.ref ?? null, files });
        talk = [...talk, msg];
        return json(msg);
      }
      if (p === '/api/v1/owner_channel/talk/files' && s.method === 'POST') {
        const name = url.searchParams.get('name') ?? 'file';
        const n = sent.filter((x) => x.path === p).length;
        return json({ id: String(n).padStart(32, 'f'), name, size: 4, mime: 'image/png', kind: 'image' });
      }
      if (p === '/api/v1/owner_channel/talk/read') return json({ marked: 1 });
      if (p === '/api/v1/owner_channel/queue') return json(queue);
      if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      if (p === '/api/v1/sessions/5') return json({ session: store.state.sessions.items[5], requests: [], says: [] });
      if (p === '/api/v1/sessions') return json({ items: Object.values(store.state.sessions.items) });
      if (p === '/api/v1/subs') return json({ items: store.state.peerMsgs.agentSubs, lines: [] });
      if (p === '/api/v1/peer-msgs' && s.method === 'POST') return json({ msg: { id: 900, text: 'x' } });
      if (p.endsWith('/say')) return json({ id: 1, session_id: 5, priority: 'next', status: 'queued' });
      return json([]);
    }),
  );
  setupStore();
});

afterEach(() => {
  closeWrite();
  layout.mode = 'full';
  resetBoard();
  resetTalk();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

function targets() {
  const groups = buildBoardList({
    projects: store.state.projects,
    sessions: Object.values(store.state.sessions.items),
    pending: {},
    agentLines: buildAgentLines({}, store.state.peerMsgs.agentSubs),
    offline: boardState.offline,
    hiddenLines: [],
    home: { react: [], drafts: [] },
    waiting: [],
    hiddenProjects: [],
    nowMs: NOW,
  });
  return buildWriteTargets({
    groups,
    agentLines: buildAgentLines({}, store.state.peerMsgs.agentSubs),
    agent: { online: true, talk_unread: 2 },
  });
}

async function mountHost() {
  const w = mount(WriteModalHost, { attachTo: document.body });
  await flushPromises();
  return w;
}

function modal(): Element | null {
  return document.body.querySelector('.write-modal');
}

function type(selector: string, value: string): void {
  const el = document.body.querySelector(selector) as HTMLTextAreaElement;
  el.value = value;
  el.dispatchEvent(new Event('input'));
}

describe('58-1 B сборка списка', () => {
  it('«Мой агент» первым, затем проекты в порядке доски, «Вне проектов» в конце', () => {
    const t = targets();
    expect(t.owner_channel.name).toBe('Мой агент');
    expect(t.owner_channel.sub).toBe('на связи');
    expect(t.owner_channel.unread).toBe(2);
    expect(t.groups.map((g) => g.name)).toEqual(['Umbrella', 'Вне проектов']);
    const rows = t.groups[0]!.rows;
    expect(rows.map((r) => r.target.kind)).toEqual(['session', 'agent', 'agent']);
    expect(rows[0]!.name).toBe('программист');
    expect(rows[0]!.mode).toMatch(/^(auto|manual)$/);
    expect(rows[1]!.mode).toBeUndefined();
    expect(rows[1]!.name).toBe('маркетолог');
    expect(rows[1]!.sub).toContain('слушает');
    expect(t.groups[1]!.rows[0]!.name).toBe('сисадмин');
  });

  it('линия «не в сети» — серой строкой с пометкой «сообщение дождётся»', () => {
    const row = targets().groups[0]!.rows.find((r) => r.name === 'аналитик')!;
    expect(row.offline).toBe(true);
    expect(row.sub).toBe('не в сети: сообщение дождётся');
  });

  it('агент не на связи — так и подписан', () => {
    const t = buildWriteTargets({ groups: [], agentLines: [], agent: { online: false, talk_unread: 0 } });
    expect(t.owner_channel.sub).toBe('не на связи');
  });

  it('поиск: по имени, роли и проекту; Enter берёт первого найденного', () => {
    const t = targets();
    expect(filterWriteTargets(t, 'маркет').groups.flatMap((g) => g.rows.map((r) => r.name))).toEqual(['маркетолог']);
    expect(filterWriteTargets(t, 'вне проектов').groups.map((g) => g.name)).toEqual(['Вне проектов']);
    expect(filterWriteTargets(t, 'umbre').groups[0]!.rows).toHaveLength(3);
    expect(firstWriteTarget(t, 'маркет')).toEqual({ kind: 'agent', key: expect.any(String) });
    expect(firstWriteTarget(t, 'мой')).toEqual({ kind: 'owner_channel' });
    expect(firstWriteTarget(t, 'нет такого')).toBeNull();
  });
});

describe('58-1 B открытие', () => {
  it('пункт меню «Написать…» открывает окно на «Моём агенте»; значок — непрочитанные', async () => {
    store.state.owner_channel.agent.talk_unread = 3;
    const menu = mount(TopMenu, { attachTo: document.body });
    await menu.get('.top-menu-button').trigger('click');
    expect(menu.get('.write-open .write-badge').text()).toBe('3');
    await menu.get('.write-open').trigger('click');
    expect(writeModal.open).toBe(true);
    const host = await mountHost();
    expect(modal()).not.toBeNull();
    expect(document.body.querySelector('.owner-talk')).not.toBeNull();
    host.unmount();
    menu.unmount();
  });

  it('видимая кнопка в верхней полосе: значок = talk_unread + ответы агентов, клик открывает окно', async () => {
    store.state.owner_channel.agent.talk_unread = 2;
    const bar = mount(TopBar, { attachTo: document.body });
    expect(bar.get('[data-test="top-write-badge"]').text()).toBe('2');
    expect(bar.get('[data-test="top-write"]').attributes('title')).toBe('Написать…');
    await bar.get('[data-test="top-write"]').trigger('click');
    expect(writeModal.open).toBe(true);
    bar.unmount();
  });

  it('кнопка в окне «Агент» открывает то же окно', async () => {
    const q = mount(AgentQueue, { attachTo: document.body });
    await flushPromises();
    await q.get('[data-test="agent-write"]').trigger('click');
    expect(writeModal.open).toBe(true);
    expect(writeModal.target).toEqual({ kind: 'owner_channel' });
    q.unmount();
  });

  it('«обсудить с агентом» у карточки: окно с плашкой, реплика уходит с ref карточки', async () => {
    const q = mount(AgentQueue, { attachTo: document.body });
    await flushPromises();
    await q.get('[data-ref="req:101"] [data-test="owner-discuss"]').trigger('click');
    expect(writeModal.card?.ref).toBe('req:101');
    const host = await mountHost();
    expect(document.body.querySelector('[data-test="talk-card"]')!.textContent).toContain('по карточке');
    type('textarea[aria-label="Написать агенту"]', 'почему так?');
    await flushPromises();
    (document.body.querySelector('.owner-talk form button[type="submit"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(posts('/owner_channel/talk')[0]!.body).toEqual({ text: 'почему так?', ref: 'req:101', session_id: 11 });
    expect(document.body.querySelector('[data-test="talk-card"]')).toBeNull();
    host.unmount();
    q.unmount();
  });
});

describe('58-1 B разговор с агентом', () => {
  it('лента, «ждёт агента», отметка прочитанного, отправка без ref', async () => {
    talk = [
      talkMsg({ id: 1, author: 'owner_channel', text: 'сделай X', handled_ts: null }),
      talkMsg({ id: 2, author: 'agent', text: 'готово', read_ts: null }),
    ];
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    const texts = [...document.body.querySelectorAll('.owner-talk .agent-msg-text')].map((e) => e.textContent?.trim());
    expect(texts).toEqual(['сделай X', 'готово']);
    expect(document.body.querySelector('[data-test="talk-waits"]')!.textContent).toBe('ждёт агента');
    expect(posts('/owner_channel/talk/read')[0]!.body).toEqual({ upto: 2, session_id: null });
    type('textarea[aria-label="Написать агенту"]', 'спасибо');
    await flushPromises();
    (document.body.querySelector('.owner-talk form button[type="submit"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(posts('/owner_channel/talk')[0]!.body).toEqual({ text: 'спасибо', session_id: null });
    expect([...document.body.querySelectorAll('.owner-talk .agent-msg-text')].map((e) => e.textContent)).toContain('спасибо');
    host.unmount();
  });

  it('файл к реплике: «📎» грузит, отправка без текста несёт files; в ленте картинка миниатюрой, прочее — ссылкой', async () => {
    talk = [
      talkMsg({
        id: 1, author: 'owner_channel', text: '', handled_ts: 5,
        files: [
          { id: 'a'.repeat(32), name: 'отчёт.pdf', size: 9, mime: 'application/pdf', kind: 'pdf', gone: false },
          { id: 'b'.repeat(32), name: 'старое.png', size: 9, mime: 'image/png', kind: 'image', gone: true },
        ],
      }),
    ];
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    const link = document.body.querySelector('[data-test="talk-files"] a') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(`/api/v1/owner_channel/talk/files/${'a'.repeat(32)}`);
    expect(link.getAttribute('download')).toBe('отчёт.pdf');
    expect(document.body.querySelector('.talk-file-gone')!.textContent).toContain('старое.png');
    const send = document.body.querySelector('.owner-talk form button[type="submit"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    const input = document.body.querySelector('[data-test="talk-file-input"]') as HTMLInputElement;
    Object.defineProperty(input, 'files', { value: [new File([new Uint8Array([137, 80, 78, 71])], 'экран.png')], configurable: true });
    input.dispatchEvent(new Event('change'));
    await flushPromises();
    expect(document.body.querySelector('.session-file-name')!.textContent).toBe('экран.png');
    expect(send.disabled).toBe(false);
    send.click();
    await flushPromises();
    expect(posts('/owner_channel/talk')[0]!.body).toEqual({ text: '', session_id: null, files: ['f'.repeat(31) + '1'] });
    expect(document.body.querySelector('.session-file-name')).toBeNull();
    const img = document.body.querySelector('img.talk-file-img') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe(`/api/v1/owner_channel/talk/files/${'f'.repeat(31)}1`);
    host.unmount();
  });

  it('строка хода агента под необработанной репликой владельца; обработанная — без неё', async () => {
    talk = [
      talkMsg({ id: 1, author: 'owner_channel', text: 'было', handled_ts: 5, progress: 'старое' }),
      talkMsg({ id: 3, author: 'owner_channel', text: 'что сегодня', handled_ts: null, progress: '1. сводки — готово, 7 с\n2. поиск — идёт 12 с' }),
    ];
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    const lines = [...document.body.querySelectorAll('[data-test="talk-progress"]')].map((e) => e.textContent);
    expect(lines).toEqual(['1. сводки — готово, 7 с\n2. поиск — идёт 12 с']);
    expect(document.body.querySelector('[data-test="talk-waits"]')!.textContent).toBe('агент работает');
    host.unmount();
  });

  it('обновление ленты не сбрасывает прокрутку, если владелец листает выше; у низа — держит низ', async () => {
    talk = [talkMsg({ id: 3, author: 'owner_channel', text: 'что сегодня', handled_ts: null, progress: '1. сводки — идёт' })];
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    const el = document.body.querySelector('.owner-talk-feed') as HTMLElement;
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 2000 });
    Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 500 });
    el.scrollTop = 300; // владелец читает выше
    store.state.owner_channel.agent.talk_progress = 111;
    await flushPromises();
    expect(el.scrollTop).toBe(300);
    el.scrollTop = 1450; // у низа
    store.state.owner_channel.agent.talk_progress = 222;
    await flushPromises();
    expect(el.scrollTop).toBe(2000);
    host.unmount();
  });

  it('агент не на связи: строка над полем, отправка всё равно доступна', async () => {
    queue.agent.online = false;
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    expect(document.body.querySelector('[data-test="talk-offline"]')!.textContent).toContain('сообщение дождётся');
    type('textarea[aria-label="Написать агенту"]', 'жду');
    await flushPromises();
    expect((document.body.querySelector('.owner-talk form button[type="submit"]') as HTMLButtonElement).disabled).toBe(false);
    host.unmount();
  });

  it('ядро без разговора (404) — подсказка, не падение', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'not found' }, 404)));
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    expect(document.body.querySelector('[data-test="talk-gone"]')).not.toBeNull();
    host.unmount();
  });
});

describe('61-1 ответ из Telegram', () => {
  it('реплика владельца из бота помечена «из Telegram», остальные — нет', async () => {
    talk = [
      talkMsg({ id: 1, author: 'agent', text: 'пароль?', ask: true, read_ts: NOW }),
      talkMsg({ id: 2, author: 'owner_channel', text: 'в менеджере', reply_to: 1, via: 'bot', handled_ts: NOW }),
      talkMsg({ id: 3, author: 'owner_channel', text: 'и ещё', via: null, handled_ts: NOW }),
    ];
    openWrite({ kind: 'owner_channel' });
    const host = await mountHost();
    const marks = [...document.body.querySelectorAll('[data-test="talk-via-bot"]')];
    expect(marks.map((e) => e.textContent)).toEqual(['из Telegram']);
    expect(marks[0]!.closest('[data-talk-id]')!.getAttribute('data-talk-id')).toBe('2');
    host.unmount();
  });
});

describe('58-1 B другие адресаты', () => {
  it('сессия хаба: окно сессии, реплика уходит say', async () => {
    openWrite({ kind: 'session', id: 5 });
    const host = await mountHost();
    expect(document.body.querySelector('.write-pane .session-window')).not.toBeNull();
    type('textarea.work-say-text', 'привет сессии');
    await flushPromises();
    (document.body.querySelector('.work-say-next') as HTMLButtonElement).click();
    await flushPromises();
    const say = sent.find((s) => s.method === 'POST' && s.path === '/api/v1/sessions/5/say')!;
    expect(say.body).toEqual({ text: 'привет сессии', priority: 'next' });
    host.unmount();
  });

  it('внешний агент: диалог AgentDialog, реплика уходит в peer-msgs', async () => {
    const key = buildAgentLines({}, store.state.peerMsgs.agentSubs).find((l) => l.name === 'маркетолог')!.key;
    openWrite({ kind: 'agent', key });
    const host = await mountHost();
    expect(document.body.querySelector('.write-pane .agent-dialog')).not.toBeNull();
    type('textarea.agent-dialog-text', 'ты тут?');
    await flushPromises();
    (document.body.querySelector('.agent-dialog form button[type="submit"]') as HTMLButtonElement).click();
    await flushPromises();
    const msg = posts('/peer-msgs')[0]!;
    expect((msg.body as { text: string }).text).toBe('ты тут?');
    host.unmount();
  });

  it('клик по строке списка переключает разговор; поиск находит и по Enter открывает', async () => {
    openWrite();
    const host = await mountHost();
    expect(document.body.querySelector('.owner-talk')).not.toBeNull();
    (document.body.querySelector('[data-write="session:5"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.write-pane .session-window')).not.toBeNull();
    type('[data-test="write-search"]', 'сисадмин');
    await flushPromises();
    expect(document.body.querySelectorAll('.write-row').length).toBe(2); // «Мой агент» и сисадмин
    document.body.querySelector('[data-test="write-search"]')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await flushPromises();
    expect(document.body.querySelector('.write-pane .agent-dialog')).not.toBeNull();
    host.unmount();
  });
});

describe('58-1 B закрытие и телефон', () => {
  it('Esc, × и клик мимо закрывают окно', async () => {
    openWrite({ kind: 'owner_channel' });
    let host = await mountHost();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(writeModal.open).toBe(false);
    expect(modal()).toBeNull();
    host.unmount();

    openWrite({ kind: 'owner_channel' });
    host = await mountHost();
    (document.body.querySelector('.channel-modal-close') as HTMLButtonElement).click();
    await flushPromises();
    expect(writeModal.open).toBe(false);
    host.unmount();

    openWrite({ kind: 'owner_channel' });
    host = await mountHost();
    (document.body.querySelector('.write-overlay') as HTMLElement).click();
    await flushPromises();
    expect(writeModal.open).toBe(false);
    host.unmount();
  });

  it('телефон: сначала список, тап — разговор, «‹» — назад к списку', async () => {
    layout.mode = 'phone';
    openWrite();
    const host = await mountHost();
    expect(document.body.querySelector('.write-list')).not.toBeNull();
    expect(document.body.querySelector('.write-pane')).toBeNull();
    expect(document.body.querySelector('[data-test="write-back"]')).toBeNull();
    (document.body.querySelector('[data-write="owner_channel"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.write-list')).toBeNull();
    expect(document.body.querySelector('.owner-talk')).not.toBeNull();
    (document.body.querySelector('[data-test="write-back"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.write-list')).not.toBeNull();
    host.unmount();
  });
});
