// Ф35-3 (`docs/architecture.md`): агенты хаба на вкладке «Claude» — редьюсер и помощники, `/sync` и кадр
// SSE, блок «Агенты», диалог владелец <-> агент, переписка агентов, счётчик на вкладках, чужой текст — как текст.
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { PeerMsg, PeerSide, Project, ProjectSub } from '../src/api/types';
import AgentDialog from '../src/components/AgentDialog.vue';
import AgentTraffic from '../src/components/AgentTraffic.vue';
import AgentsBlock from '../src/components/AgentsBlock.vue';
import ClaudeView from '../src/components/ClaudeView.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';
import {
  agentTraffic,
  applyPeerMsg,
  buildAgentLines,
  createPeerMsgState,
  dialogWith,
  lineKey,
  modeLabel,
  statusLabel,
  unreadMinutes,
  unreadRepliesCount,
} from '../src/store/peerMsgs';
import { createStaleness } from '../src/store/staleness';
import { createStore } from '../src/store/store';
import { connectStream, type EventSourceLike } from '../src/store/stream';

const NOW = new Date(2026, 9, 3, 15, 0, 0).getTime();
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function side(overrides: Partial<PeerSide> = {}): PeerSide {
  return { project_id: 3, role: 'маркетолог', role_no: 1, sub_id: 31, ...overrides };
}

function msg(overrides: Partial<PeerMsg> = {}): PeerMsg {
  return {
    id: 1,
    from_user: false,
    to_user: false,
    from: side(),
    to: side({ role: 'аналитик', sub_id: 32 }),
    text: 'привет',
    reply_to: null,
    chain_id: null,
    hop: 0,
    status: 'read',
    reason: null,
    created_ts: NOW - 60_000,
    signaled_ts: null,
    signals: 0,
    read_ts: null,
    closed_ts: null,
    expires_ts: null,
    seq: 1,
    ...overrides,
  };
}

function sub(overrides: Partial<ProjectSub> = {}): ProjectSub {
  return {
    id: 31,
    project_id: 3,
    role: 'маркетолог',
    role_no: 1,
    mode: 'cards',
    engine: 'claude',
    session_name: null,
    live: true,
    listening: true,
    waiting: true,
    ...overrides,
  };
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: null, unread: 0, ...overrides };
}

function toUser(overrides: Partial<PeerMsg> = {}): PeerMsg {
  return msg({ from_user: false, to_user: true, from: side(), to: null, status: 'pending', ...overrides });
}
function fromUser(overrides: Partial<PeerMsg> = {}): PeerMsg {
  return msg({ from_user: true, to_user: false, from: null, to: side(), status: 'pending', ...overrides });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  query: string;
  body: unknown;
}

function fakeFetch(handler: (s: Sent) => Response | undefined = () => undefined) {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: url.pathname,
      query: url.search,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    sent.push(s);
    const custom = handler(s);
    if (custom !== undefined) return custom;
    if (s.path === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (s.path === '/api/v1/status') return jsonResponse({ accounts: [] });
    if (s.path === '/api/v1/subs') return jsonResponse({ items: [], channels: [] });
    if (s.path === '/api/v1/peer-msgs' && s.method === 'GET') return jsonResponse({ items: [] });
    if (s.path === '/api/v1/peer-msgs/read') return jsonResponse({ marked: 1 });
    if (s.method === 'GET') return jsonResponse([]);
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

enableAutoUnmount(afterEach);

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project(), project({ id: 1, name: 'Globex', color: '#00ff00' })];
  store.state.homeOpen = false;
  store.state.claudeOpen = true;
  delete (window as unknown as { __xss?: number }).__xss;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('Ф35-3: редьюсер и помощники', () => {
  it('старший seq побеждает', () => {
    const st = createPeerMsgState();
    applyPeerMsg(st, msg({ seq: 5, status: 'pending' }));
    applyPeerMsg(st, msg({ seq: 4, status: 'lost' }));
    expect(st.items[1]!.status).toBe('pending');
    applyPeerMsg(st, msg({ seq: 6, status: 'read' }));
    expect(st.items[1]!.status).toBe('read');
  });

  it('lineKey: роль без регистра, ё=е, пробелы; безрольный и проект 0 — по подписке', () => {
    expect(lineKey(side({ role: '  Маркетолог  ', role_no: 2 }))).toBe('p3:маркетолог:2');
    expect(lineKey(side({ role: 'Всё  так', role_no: 1 }))).toBe(lineKey(side({ role: 'все так', role_no: 1 })));
    expect(lineKey(side({ role: null, role_no: 1, sub_id: 40 }))).toBe('s40');
    expect(lineKey(side({ project_id: 0, role: 'агент', sub_id: 41 }))).toBe('s41');
    expect(lineKey({ ...sub({ id: 50, project_id: 0, role: 'x' }) })).toBe('s50');
  });

  it('диалог владельца с линией — по возрастанию; чужие и агентские строки не входят; счётчик непрочитанных', () => {
    const st = createPeerMsgState();
    for (const m of [
      toUser({ id: 5, status: 'pending' }),
      fromUser({ id: 2 }),
      fromUser({ id: 3, to: side({ role: 'аналитик', sub_id: 32 }) }),
      toUser({ id: 4, status: 'read' }),
      msg({ id: 6 }),
    ]) applyPeerMsg(st, m);
    const key = lineKey(side());
    expect(dialogWith(st.items, key).map((m) => m.id)).toEqual([2, 4, 5]);
    expect(agentTraffic(st.items).map((m) => m.id)).toEqual([6]);
    expect(unreadRepliesCount(st.items)).toBe(1);
    expect(unreadRepliesCount(st.items, key)).toBe(1);
    expect(unreadRepliesCount(st.items, 'p9:x:1')).toBe(0);
  });

  it('подписи статуса', () => {
    expect(statusLabel(msg({ status: 'pending', signals: 0 }))).toBe('ждёт');
    expect(statusLabel(msg({ status: 'pending', signals: 2 }))).toBe('доставлено, не прочитано');
    expect(statusLabel(msg({ status: 'read' }))).toBe('прочитано');
    expect(statusLabel(msg({ status: 'lost' }))).toBe('не доставлено — сессия завершилась');
    expect(statusLabel(msg({ status: 'expired' }))).toBe('просрочено');
  });

  it('непрочитанное агентом дольше 20 минут — минуты; ответ владельцу и свежее — нет', () => {
    expect(unreadMinutes(msg({ status: 'pending', created_ts: NOW - 25 * 60_000 }), NOW)).toBe(25);
    expect(unreadMinutes(msg({ status: 'pending', created_ts: NOW - 10 * 60_000 }), NOW)).toBeNull();
    expect(unreadMinutes(toUser({ created_ts: NOW - 99 * 60_000 }), NOW)).toBeNull();
  });

  it('buildAgentLines: номер при двух одной роли, «без роли N», вне проектов с папкой, «не в сети»', () => {
    const st = createPeerMsgState();
    applyPeerMsg(st, fromUser({ id: 1, to: side({ role: 'аналитик', role_no: 1, sub_id: 99 }) }));
    const lines = buildAgentLines(st.items, [
      sub({ id: 1, role_no: 1 }),
      sub({ id: 2, role_no: 2 }),
      sub({ id: 3, role: null, role_no: 4 }),
      sub({ id: 4, project_id: 0, role: null, role_no: 1, mode: 'direct', cwd: '/home/u/x' }),
      sub({ id: 5, project_id: 1, role: 'ревью', role_no: 1 }),
    ]);
    expect(lines.map((l) => l.name)).toEqual(['маркетолог 1', 'маркетолог 2', 'без роли 4', 'без роли 1', 'ревью', 'аналитик']);
    expect(lines[3]!.cwd).toBe('/home/u/x');
    // привязанные линии (безрольный, вне проектов) — с подпиской: номер может занять другая сессия
    expect(lines[2]!.address).toEqual({ project_id: 3, number: 4, sub_id: 3 });
    expect(lines[3]!.address).toEqual({ project_id: 0, number: 1, sub_id: 4 });
    expect(lines[0]!.address).toEqual({ project_id: 3, role: 'маркетолог', number: 1 });
    expect(lines[5]!.sub).toBeNull();
  });
});

describe('Ф35-3: стор — /sync и SSE', () => {
  it('/sync.peer_msgs попадает в стор, старший seq побеждает', async () => {
    let calls = 0;
    const { fetchMock } = fakeFetch((s) => {
      if (s.path !== '/api/v1/sync') return undefined;
      calls += 1;
      const rows = calls === 1 ? [msg({ seq: 11, status: 'pending' })] : [msg({ seq: 9, status: 'lost' })];
      return jsonResponse({ messages: [], channels: [], max_seq: calls + 1, more: false, peer_msgs: rows });
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.runSync();
    expect(s.state.peerMsgs.items[1]!.status).toBe('pending');
    await s.runSync();
    expect(s.state.peerMsgs.items[1]!.status).toBe('pending');
  });

  it('кадр SSE peer_msg запускает /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      readyState: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      close: () => {},
    };
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('peer_msg')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
  });
});

describe('Ф38-1: фильтр подписки по аккаунтам рядом с режимом', () => {
  it('«только» и «кроме»; без фильтра и при кривом значении — как раньше; у direct не печатается', () => {
    expect(modeLabel(sub({ mode: 'cards', accounts: { only: [1, 5] } }))).toBe('карточки · только аккаунты 1, 5');
    expect(modeLabel(sub({ mode: 'messages', msg_filter: 'personal', accounts: { except: [8] } }))).toBe(
      'лично мне · кроме аккаунтов 8',
    );
    expect(modeLabel(sub({ mode: 'messages', msg_filter: 'all', accounts: { only: [8] } }))).toBe(
      'каждое сообщение · только аккаунты 8',
    );
    expect(modeLabel(sub({ mode: 'cards', accounts: null }))).toBe('карточки');
    expect(modeLabel(sub({ mode: 'cards' }))).toBe('карточки');
    expect(modeLabel(sub({ mode: 'direct', accounts: { only: [8] } }))).toBe('только сообщения агентов');
    for (const broken of [{}, { only: [] }, { only: ['8'] }, { only: [1], except: [8] }, [8], { both: [8] }]) {
      expect(modeLabel(sub({ mode: 'cards', accounts: broken as never }))).toBe('карточки');
    }
  });

  it('в блоке «Агенты» — в строке агента', () => {
    store.state.peerMsgs.agentSubs = [sub({ id: 1, role_no: 1, accounts: { except: [8] } })];
    const row = mount(AgentsBlock, { global: { stubs: { teleport: true } } }).get('.agent-row').text();
    expect(row).toContain('карточки · кроме аккаунтов 8');
  });
});

describe('Ф41-1: срок кэша в строке агента', () => {
  it('строка «кэш … · контекст …» только когда cache есть, время — от nowMs', () => {
    const cache = { last_ts: NOW - 20 * 60_000, ttl_s: 3600, expires_ts: NOW + 40 * 60_000, context_tokens: 143_200, model: 'm' };
    store.state.peerMsgs.agentSubs = [sub({ id: 1, cache }), sub({ id: 2, role_no: 2, engine: 'opencode' })];
    const wrapper = mount(AgentsBlock, { props: { nowMs: NOW }, global: { stubs: { teleport: true } } });
    const lines = wrapper.findAll('.cache-line');
    expect(lines).toHaveLength(1);
    expect(lines[0].text()).toBe('кэш 40 мин · контекст 143 тыс.');
    expect(lines[0].attributes('title')).toBe('кэш 1 час');
  });
});

describe('Ф35-3: блок «Агенты»', () => {
  it('группы по проектам, имя с номером, «вне проектов» с папкой, «не в сети», счётчик ответов', async () => {
    store.state.peerMsgs.agentSubs = [
      sub({ id: 1, role_no: 1, listening: false, mode: 'messages', msg_filter: 'personal', engine: 'opencode' }),
      sub({ id: 2, role_no: 2 }),
      sub({ id: 4, project_id: 0, role: null, role_no: 1, mode: 'direct', cwd: '/home/u/x' }),
    ];
    applyPeerMsg(store.state.peerMsgs, toUser({ id: 7, from: side({ role_no: 2, sub_id: 2 }) }));
    applyPeerMsg(store.state.peerMsgs, fromUser({ id: 8, to: side({ project_id: 1, role: 'ревью', sub_id: 9 }) }));
    const wrapper = mount(AgentsBlock, { global: { stubs: { teleport: true } } });
    const groups = wrapper.findAll('.agents-group');
    expect(groups.map((g) => g.get('.agents-group-name').text())).toEqual(['Umbrella', 'Globex', 'Вне проектов']);
    const rows = wrapper.findAll('.agent-row').map((r) => r.text());
    expect(rows[0]).toContain('маркетолог 1');
    expect(rows[0]).toContain('OpenCode');
    expect(rows[0]).toContain('не слушает');
    expect(rows[0]).toContain('лично мне');
    expect(rows[1]).toContain('маркетолог 2');
    expect(rows[1]).toContain('Claude Code');
    expect(rows[1]).toContain('слушает');
    expect(rows[1]).toContain('карточки');
    expect(wrapper.get('.agent-unread').text()).toBe('1');
    expect(rows[2]).toContain('ревью');
    expect(rows[2]).toContain('не в сети');
    expect(rows[3]).toContain('без роли 1');
    expect(rows[3]).toContain('только сообщения агентов');
    expect(rows[3]).toContain('/home/u/x');
  });

  it('пусто — «Агентов в сети нет»', () => {
    expect(mount(AgentsBlock, { global: { stubs: { teleport: true } } }).text()).toContain('Агентов в сети нет');
  });

  it('клик раскрывает диалог, и непрочитанные ответы отмечаются прочитанными', async () => {
    const { sent, fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    store.state.peerMsgs.agentSubs = [sub()];
    applyPeerMsg(store.state.peerMsgs, toUser({ id: 7, text: 'ответ агента' }));
    const wrapper = mount(AgentsBlock, { global: { stubs: { teleport: true } } });
    expect(wrapper.find('.agent-dialog').exists()).toBe(false);
    await wrapper.get('.agent-row').trigger('click');
    await flushPromises();
    expect(wrapper.get('.agent-dialog').text()).toContain('ответ агента');
    const read = sent.find((s) => s.path === '/api/v1/peer-msgs/read')!;
    expect(read.body).toEqual({ ids: [7] });
    expect(store.state.peerMsgs.items[7]!.status).toBe('read');
    await wrapper.get('.agent-row').trigger('click');
    expect(wrapper.find('.agent-dialog').exists()).toBe(false);
  });

  it('роль, имя проекта, папка и текст с HTML выводятся как текст', async () => {
    vi.stubGlobal('fetch', fakeFetch().fetchMock);
    store.state.projects = [project({ name: XSS })];
    store.state.peerMsgs.agentSubs = [sub({ role: XSS }), sub({ id: 4, project_id: 0, role: null, role_no: 1, cwd: XSS })];
    applyPeerMsg(store.state.peerMsgs, toUser({ text: XSS, from: side({ role: XSS }) }));
    const wrapper = mount(AgentsBlock, { attachTo: document.body, global: { stubs: { teleport: true } } });
    await wrapper.get('.agent-row').trigger('click');
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.find('script').exists()).toBe(false);
    expect(wrapper.text()).toContain(XSS);
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
  });
});

describe('Ф35-3: диалог', () => {
  function line(overrides: Partial<Parameters<typeof buildAgentLines>[1][number]> = {}, items = store.state.peerMsgs.items) {
    return buildAgentLines(items, [sub(overrides)])[0]!;
  }

  it('линия с ролью: тело запроса — {project_id, role, number}; Ctrl+Enter отправляет, фокус остаётся, поле очищается', async () => {
    const { sent, fetchMock } = fakeFetch((s) => {
      if (s.method === 'POST' && s.path === '/api/v1/peer-msgs') {
        return jsonResponse({ msg: fromUser({ id: 20, text: 'делай' }), recipient: { live: true, waiting: true } }, 201);
      }
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(AgentDialog, { props: { line: line() }, attachTo: document.body });
    const area = wrapper.get('textarea');
    await area.setValue('делай');
    await area.trigger('keydown', { key: 'Enter', ctrlKey: true });
    await flushPromises();
    const post = sent.find((s) => s.method === 'POST' && s.path === '/api/v1/peer-msgs')!;
    expect(post.body).toEqual({ text: 'делай', to: { project_id: 3, role: 'маркетолог', number: 1 } });
    expect((area.element as HTMLTextAreaElement).value).toBe('');
    expect(document.activeElement).toBe(area.element);
    expect(wrapper.text()).toContain('делай');
    expect(wrapper.text()).toContain('ждёт');
  });

  it('безрольный: тело — {project_id, number, sub_id}; кнопка тоже отправляет', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.method === 'POST' && s.path === '/api/v1/peer-msgs'
        ? jsonResponse({ msg: fromUser({ id: 21 }), recipient: { live: true, waiting: false } }, 201)
        : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(AgentDialog, { props: { line: line({ id: 77, role: null, role_no: 4 }) } });
    await wrapper.get('textarea').setValue('привет');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(sent.find((s) => s.method === 'POST')!.body).toEqual({
      text: 'привет',
      to: { project_id: 3, number: 4, sub_id: 77 },
    });
  });

  it('агент вне проектов: адрес с подпиской; отказ ядра перечитывает список агентов', async () => {
    const { sent, fetchMock } = fakeFetch((s) => {
      if (s.method === 'POST' && s.path === '/api/v1/peer-msgs') return jsonResponse({ detail: 'эта сессия уже не в сети' }, 409);
      if (s.method === 'GET' && s.path === '/api/v1/subs') return jsonResponse({ items: [] });
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    const free = line({ id: 78, project_id: 0, role: 'хаб', role_no: 1, mode: 'direct' });
    const wrapper = mount(AgentDialog, { props: { line: free } });
    await wrapper.get('textarea').setValue('сделай пуш');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(sent.find((s) => s.method === 'POST')!.body).toEqual({
      text: 'сделай пуш',
      to: { project_id: 0, role: 'хаб', number: 1, sub_id: 78 },
    });
    expect(wrapper.get('[role="alert"]').text()).toBe('эта сессия уже не в сети');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('сделай пуш');
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/subs')).toBe(true);
  });

  it('ошибка ядра — текстом под полем, введённое не теряется; 429 с объектом — не «[object Object]»', async () => {
    let status = 409;
    const { fetchMock } = fakeFetch((s) => {
      if (s.method !== 'POST') return undefined;
      return status === 409
        ? jsonResponse({ detail: 'агент не в сети' }, 409)
        : jsonResponse({ detail: { detail: 'слишком много', blocked_id: 3 } }, 429);
    });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(AgentDialog, { props: { line: line() } });
    await wrapper.get('textarea').setValue('текст');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe('агент не в сети');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('текст');
    status = 429;
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toBe('слишком много');
  });

  it('линия с ролью не в сети: поле включено, подсказка про двое суток; безрольный не в сети — поле выключено', () => {
    const offlineRole = buildAgentLines(
      { 1: fromUser({ id: 1 }) },
      [],
    )[0]!;
    expect(offlineRole.sub).toBeNull();
    const w1 = mount(AgentDialog, { props: { line: offlineRole } });
    expect(w1.get('textarea').attributes('disabled')).toBeUndefined();
    expect(w1.text()).toContain('получит сессия, которая подпишется на эту линию; ждёт двое суток');
    const noRole = { ...offlineRole, hasRole: false, key: 's9', address: { project_id: 3, number: 9 } };
    const w2 = mount(AgentDialog, { props: { line: noRole } });
    expect(w2.get('textarea').attributes('disabled')).toBeDefined();
    expect(w2.text()).toContain('написать можно только пока агент в сети');
  });
});

describe('Ф35-3: переписка агентов', () => {
  it('новые сверху, «не прочитано N мин», пусто — подпись', () => {
    expect(mount(AgentTraffic, { props: { nowMs: NOW } }).text()).toContain('Агенты между собой не переписывались');
    applyPeerMsg(store.state.peerMsgs, msg({ id: 1, text: 'первое', status: 'pending', created_ts: NOW - 30 * 60_000 }));
    applyPeerMsg(store.state.peerMsgs, msg({ id: 2, text: 'второе', status: 'read' }));
    applyPeerMsg(store.state.peerMsgs, toUser({ id: 3, text: 'ответ владельцу' }));
    const wrapper = mount(AgentTraffic, { props: { nowMs: NOW } });
    const rows = wrapper.findAll('.traffic-row');
    expect(rows.map((r) => r.get('.traffic-text').text())).toEqual(['второе', 'первое']);
    expect(rows[0]!.text()).toContain('прочитано');
    expect(rows[1]!.text()).toContain('не прочитано 30 мин');
    expect(rows[1]!.text()).toContain('Umbrella / маркетолог');
    expect(rows[1]!.text()).toContain('→');
  });

  it('«ещё» догружает по before', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/peer-msgs' && s.method === 'GET'
        ? jsonResponse({ items: [msg({ id: 1, text: 'старое' })] })
        : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    applyPeerMsg(store.state.peerMsgs, msg({ id: 5, text: 'новое' }));
    const wrapper = mount(AgentTraffic, { props: { nowMs: NOW } });
    await wrapper.get('.traffic-more').trigger('click');
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/peer-msgs')!.query).toContain('before=5');
    expect(wrapper.findAll('.traffic-text').map((n) => n.text())).toEqual(['новое', 'старое']);
  });

  it('текст с HTML — как текст', () => {
    applyPeerMsg(store.state.peerMsgs, msg({ text: XSS }));
    const wrapper = mount(AgentTraffic, { props: { nowMs: NOW }, attachTo: document.body });
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.get('.traffic-text').text()).toBe(XSS);
  });
});

describe('Ф35-3: вкладка и счётчик', () => {
  it('ClaudeView: блоки между чатом оркестратора и секциями, данные грузятся', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/subs' ? jsonResponse({ items: [sub()], channels: [] }) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(ClaudeView, { props: { phone: true } }); // Ф41-6a: чат оркестратора — только у телефона
    await flushPromises();
    const kids = Array.from(wrapper.get('main.claude-view').element.children).map((e) => e.getAttribute('aria-label') ?? e.className);
    expect(kids.indexOf('Агенты')).toBeGreaterThan(kids.findIndex((k) => k.includes('orch-chat')));
    expect(kids.indexOf('Переписка агентов')).toBe(kids.indexOf('Агенты') + 1);
    expect(sent.some((s) => s.path === '/api/v1/peer-msgs')).toBe(true);
    expect(wrapper.find('.agent-row').exists()).toBe(true);
  });

  it('счётчик непрочитанных ответов — в пункте меню «Диспетчерская» десктопа и на вкладке «Claude» телефона', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    applyPeerMsg(store.state.peerMsgs, toUser({ id: 1 }));
    applyPeerMsg(store.state.peerMsgs, toUser({ id: 2 }));
    const top = mount(TopBar);
    await top.get('.top-menu-button').trigger('click');
    expect(top.get('.dispatch-open .agent-replies-badge').text()).toBe('2');
    const phone = mount(PhoneShell);
    expect(phone.get('.phone-tab--orch .agent-replies-badge').text()).toBe('2');
    store.state.peerMsgs = createPeerMsgState();
    await flushPromises();
    expect(top.find('.agent-replies-badge').exists()).toBe(false);
  });
});

describe('Ф36-2: диалог переживает смену роли и перерисовку', () => {
  // безрольная подписка 77 сменила роль на месте: ключ s77 -> p3:аналитик:1, sub_id тот же
  const oldTo = (): PeerSide => side({ role: null, role_no: 4, sub_id: 77 });
  const newTo = (): PeerSide => side({ role: 'аналитик', role_no: 1, sub_id: 77 });

  it('dialogWith и непрочитанные: сообщения подписки по sub_id; «призрака» нет', () => {
    const items: Record<number, PeerMsg> = {};
    applyPeerMsg({ ...createPeerMsgState(), items }, fromUser({ id: 1, to: oldTo() }));
    items[2] = fromUser({ id: 2, to: newTo() });
    items[3] = toUser({ id: 3, from: oldTo() });
    items[4] = fromUser({ id: 4, to: side({ role: 'чужой', sub_id: 90 }) });
    expect(dialogWith(items, 'p3:аналитик:1').map((m) => m.id)).toEqual([2]);
    expect(dialogWith(items, 'p3:аналитик:1', 77).map((m) => m.id)).toEqual([1, 2, 3]);
    expect(unreadRepliesCount(items, 'p3:аналитик:1')).toBe(0);
    expect(unreadRepliesCount(items, 'p3:аналитик:1', 77)).toBe(1);
    const live = sub({ id: 77, role: 'аналитик', role_no: 1 });
    const lines = buildAgentLines({ ...items, 5: fromUser({ id: 5, to: side({ role: 'старая', sub_id: 77 }) }) }, [live]);
    expect(lines.map((l) => l.key)).toEqual(['p3:аналитик:1', 'p3:чужой:1']);
    expect(lines[0]!.unread).toBe(1);
  });

  it('смена роли при открытом диалоге: диалог остаётся, история на месте, текст не пропадает', async () => {
    vi.stubGlobal('fetch', fakeFetch().fetchMock);
    store.state.peerMsgs.agentSubs = [sub({ id: 77, role: null, role_no: 4 })];
    applyPeerMsg(store.state.peerMsgs, fromUser({ id: 1, text: 'старая реплика', to: oldTo() }));
    const wrapper = mount(AgentsBlock, { global: { stubs: { teleport: true } } });
    await wrapper.get('.agent-row').trigger('click');
    await wrapper.get('textarea').setValue('черновик');
    store.state.peerMsgs.agentSubs = [sub({ id: 77, role: 'аналитик', role_no: 1 })];
    applyPeerMsg(store.state.peerMsgs, fromUser({ id: 2, text: 'новая реплика', to: newTo() }));
    await flushPromises();
    expect(wrapper.find('.agent-dialog').exists()).toBe(true);
    expect(wrapper.get('.agent-name').text()).toBe('аналитик');
    const text = wrapper.get('.agent-dialog').text();
    expect(text).toContain('старая реплика');
    expect(text).toContain('новая реплика');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('черновик');
  });

  it('отказ отправки: текст и ошибка видны после перечитывания списка, и когда линия исчезла', async () => {
    const { fetchMock } = fakeFetch((s) => {
      if (s.method === 'POST' && s.path === '/api/v1/peer-msgs') return jsonResponse({ detail: 'эта сессия уже не в сети' }, 409);
      if (s.method === 'GET' && s.path === '/api/v1/subs') return jsonResponse({ items: [] });
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.peerMsgs.agentSubs = [sub({ id: 77, role: null, role_no: 4 })];
    const wrapper = mount(AgentsBlock, { global: { stubs: { teleport: true } } });
    await wrapper.get('.agent-row').trigger('click');
    await wrapper.get('textarea').setValue('важный текст');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    // список перечитан пустым: строка линии размонтирована
    expect(wrapper.find('.agent-row').exists()).toBe(false);
    const orphan = wrapper.get('.agent-orphan');
    expect(orphan.text()).toContain('важный текст');
    expect(orphan.text()).toContain('эта сессия уже не в сети');
    await orphan.get('button').trigger('click');
    expect(wrapper.find('.agent-orphan').exists()).toBe(false);
  });

  it('отказ отправки, линия осталась: ошибка и текст на месте после перечитывания', async () => {
    const { fetchMock } = fakeFetch((s) => {
      if (s.method === 'POST' && s.path === '/api/v1/peer-msgs') return jsonResponse({ detail: 'занято' }, 409);
      if (s.method === 'GET' && s.path === '/api/v1/subs') return jsonResponse({ items: [sub({ id: 77, role: 'аналитик', role_no: 1 })] });
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    store.state.peerMsgs.agentSubs = [sub({ id: 77, role: null, role_no: 4 })];
    const wrapper = mount(AgentsBlock, { global: { stubs: { teleport: true } } });
    await wrapper.get('.agent-row').trigger('click');
    await wrapper.get('textarea').setValue('текст');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(wrapper.find('.agent-orphan').exists()).toBe(false);
    expect(wrapper.get('[role="alert"]').text()).toBe('занято');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('текст');
  });
});

describe('Ф36-2: самообновление списка агентов', () => {
  it('таймер 30 с и visibilitychange обновляют не чаще раза в 5 с; при размонтировании снимаются', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    const { sent, fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const subsCalls = (): number => sent.filter((s) => s.path === '/api/v1/subs').length;
    const st = createStore();
    const stop = st.watchAgentSubs();
    vi.advanceTimersByTime(30_000);
    await flushPromises();
    expect(subsCalls()).toBe(1);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(subsCalls()).toBe(1); // раньше 5 с — не чаще
    vi.advanceTimersByTime(6_000);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(subsCalls()).toBe(2);
    stop();
    vi.advanceTimersByTime(120_000);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(subsCalls()).toBe(2);
  });

  it('/sync с сообщениями агентов обновляет список, пока есть наблюдатель; без него — нет', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/sync'
        ? jsonResponse({ messages: [], channels: [], peer_msgs: [msg({ id: 5, seq: 2 })], max_seq: 2, more: false })
        : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const subsCalls = (): number => sent.filter((s) => s.path === '/api/v1/subs').length;
    const st = createStore();
    await st.runSync();
    expect(subsCalls()).toBe(0);
    const stop = st.watchAgentSubs();
    await st.runSync();
    await flushPromises();
    expect(subsCalls()).toBe(1);
    stop();
  });

  it('ClaudeView снимает слушатель при размонтировании', async () => {
    vi.stubGlobal('fetch', fakeFetch().fetchMock);
    const add = vi.spyOn(document, 'addEventListener');
    const remove = vi.spyOn(document, 'removeEventListener');
    const wrapper = mount(ClaudeView);
    await flushPromises();
    expect(add.mock.calls.some((c) => c[0] === 'visibilitychange')).toBe(true);
    wrapper.unmount();
    expect(remove.mock.calls.some((c) => c[0] === 'visibilitychange')).toBe(true);
  });
});
