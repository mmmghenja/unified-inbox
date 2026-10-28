// Ф41-5 (интерфейс, разделы 6 и 12 спеки): заявки агентов — карточки в «Нужна реакция» проекта и во «Ждёт», кнопки
// «Перенести» / «Завести» / «Изменить и завести» / «Нет», отказ 409 текстом, число «Ждёт тебя» одинаково на
// компьютере и телефоне, ручки нет (404) — без ошибок; подписи «без подтверждения · роль · автопилот» и
// «принято автопилотом».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { AgentRequest, LeaveRequest, Project } from '../src/api/types';
import AgentRequestCard from '../src/components/AgentRequestCard.vue';
import LeaveCard from '../src/components/LeaveCard.vue';
import MessageRow from '../src/components/MessageRow.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createAgentRequestState } from '../src/store/agentRequests';
import { resetRoles } from '../src/store/roles';
import { initLayout } from '../src/util/layout';
import { buildBoard } from '../src/util/board';
import { buildWaiting } from '../src/util/waiting';
import { waitingInputOf } from '../src/util/waitInput';
import { moveHeadline, partnerHeadline } from '../src/util/agentRequests';
import { fakeMedia } from './media';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

let sent: Sent[] = [];
let listAnswer: () => Response = () => json({ detail: 'not found' }, 404);
let approveAnswer: () => Response = () => json({ request: { ...move(), status: 'approved' } });
let declineAnswer: () => Response = () => json({ request: { ...move(), status: 'declined' } });

function stubFetch(): void {
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      sent.push({ method, path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const p = url.pathname;
      if (p === '/api/v1/agent-requests') return listAnswer();
      if (/^\/api\/v1\/agent-requests\/\d+\/approve$/.test(p)) return approveAnswer();
      if (/^\/api\/v1\/agent-requests\/\d+\/decline$/.test(p)) return declineAnswer();
      if (p === '/api/v1/roles') return json({ detail: 'not found' }, 404);
      if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      return json([]);
    }),
  );
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true, ...overrides } as Project;
}

function move(overrides: Partial<AgentRequest> = {}): AgentRequest {
  return {
    id: 11,
    seq: 1,
    kind: 'move_channel',
    project_id: 3,
    who: 'маркетолог',
    reason: 'это клиент проекта',
    status: 'pending',
    auto: false,
    note: null,
    created_ts: NOW,
    decided_ts: null,
    payload: { channel_id: 283, channel_title: 'Покупатель', from_project_id: 4 },
    ...overrides,
  };
}

function partner(overrides: Partial<AgentRequest> = {}): AgentRequest {
  return {
    id: 12,
    seq: 1,
    kind: 'new_session',
    project_id: 3,
    who: 'маркетолог 2',
    reason: null,
    status: 'pending',
    auto: false,
    note: null,
    created_ts: NOW,
    decided_ts: null,
    payload: { role: 'тестер', task: 'проверь форму', engine: 'claude', model: null, effort: null, mode: 'manual' },
    ...overrides,
  };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  listAnswer = () => json({ detail: 'not found' }, 404);
  approveAnswer = () => json({ request: { ...move(), status: 'approved' } });
  declineAnswer = () => json({ request: { ...move(), status: 'declined' } });
  resetRoles();
  stubFetch();
  store.state.agentRequests = createAgentRequestState();
  store.state.projects = [project()];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
  store.state.syncError = null;
});

afterEach(() => {
  initLayout(null);
  store.state.phoneTab = 'wait';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-5: заявки агентов — тексты и счёт', () => {
  it('заголовки: перенос и напарник', () => {
    expect(moveHeadline(move(), 'Umbrella')).toBe('маркетолог просит перенести беседу «Покупатель» в «Umbrella»');
    expect(partnerHeadline(partner())).toBe('маркетолог 2 просит напарника: «тестер», Claude Code, ручной — проверь форму');
  });

  it('вид WaitItem `agent_request` входит в «Ждёт тебя»; решённые не считаются', () => {
    store.state.agentRequests.items = { 11: move(), 12: partner({ status: 'approved' }) };
    const items = buildWaiting(waitingInputOf(store));
    expect(items.map((i) => i.kind)).toEqual(['agent_request']);
  });

  it('число «Ждёт» одинаково на компьютере (buildWaiting) и на телефоне (бейдж вкладки) на одном наборе', async () => {
    store.state.agentRequests.items = { 11: move(), 12: partner() };
    const desktop = buildWaiting(waitingInputOf(store)).length;
    expect(desktop).toBe(2);
    initLayout(fakeMedia(390).matchMedia);
    const w = mount(PhoneShell);
    await flushPromises();
    expect(w.get('.phone-tab--wait .badge').text()).toBe(String(desktop));
    w.unmount();
  });

  it('столбец проекта на доске получает ждущие заявки и считает их в «ждёт»', () => {
    const board = buildBoard(store.state.projects, { react: [], drafts: [], runs: [], sent: [], agentRequests: [move(), partner({ status: 'declined' })] }, NOW);
    const column = board.columns[0]!;
    expect(column.agentRequests.map((r) => r.id)).toEqual([11]);
    expect(column.waitCount).toBe(1);
  });
});

describe('Ф41-5: карточка заявки', () => {
  it('перенос: «Перенести» -> approve; «Нет» -> decline', async () => {
    const w = mount(AgentRequestCard, { props: { request: move() } });
    expect(w.text()).toContain('просит перенести беседу «Покупатель» в «Umbrella»');
    expect(w.text()).toContain('Причина: это клиент проекта');
    await w.get('.agent-request-approve').trigger('click');
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/agent-requests/11/approve')?.method).toBe('POST');
    expect(store.state.agentRequests.items[11]?.status).toBe('approved');
    await w.get('.agent-request-decline').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.path === '/api/v1/agent-requests/11/decline')).toBe(true);
    w.unmount();
  });

  it('напарник: «Завести» шлёт approve без изменений', async () => {
    const w = mount(AgentRequestCard, { props: { request: partner() } });
    expect(w.text()).toContain('просит напарника: «тестер»');
    await w.get('.agent-request-approve').trigger('click');
    await flushPromises();
    const post = sent.find((s) => s.path === '/api/v1/agent-requests/12/approve');
    expect(post?.body).toEqual({ subdir: '', role_file: '' }); // Ф50-7: оба ключа всегда
    w.unmount();
  });

  it('напарник: «Изменить и завести» — форма с полями заявки и флажком autosend_ok', async () => {
    approveAnswer = () => json({ request: { ...partner(), status: 'approved' } });
    const w = mount(AgentRequestCard, { props: { request: partner() } });
    await w.get('.agent-request-edit').trigger('click');
    await flushPromises();
    expect((w.get('.session-new-role').element as HTMLInputElement).value).toBe('тестер');
    expect((w.get('.session-new-mode').element as HTMLSelectElement).value).toBe('manual');
    await w.get('.session-new-mode').setValue('auto');
    await w.get('.session-new-autosend').setValue(true);
    await w.get('form').trigger('submit');
    await flushPromises();
    const post = sent.find((s) => s.path === '/api/v1/agent-requests/12/approve');
    expect(post?.body).toMatchObject({ role: 'тестер', engine: 'claude', mode: 'auto', autosend_ok: true });
    w.unmount();
  });

  it('напарник: «Нет» открывает поле причины, decline уходит с note', async () => {
    declineAnswer = () => json({ request: { ...partner(), status: 'declined' } });
    const w = mount(AgentRequestCard, { props: { request: partner() } });
    await w.get('.agent-request-decline').trigger('click');
    await w.get('.agent-request-note-input').setValue('не сейчас');
    await w.get('.agent-request-decline-send').trigger('click');
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/agent-requests/12/decline')?.body).toEqual({ note: 'не сейчас' });
    w.unmount();
  });

  it('отказ ядра 409 — текст причины в карточке, заявка остаётся ждущей', async () => {
    approveAnswer = () => json({ detail: 'потолок 8 сессий на проект' }, 409);
    const w = mount(AgentRequestCard, { props: { request: partner() } });
    await w.get('.agent-request-approve').trigger('click');
    await flushPromises();
    expect(w.get('.agent-request-error').text()).toBe('потолок 8 сессий на проект');
    w.unmount();
  });

  it('карточка во вкладке «Ждёт» телефона', async () => {
    store.state.agentRequests.items = { 11: move() };
    const w = mount(WaitList);
    await flushPromises();
    expect(w.find('.wait-item--agent_request .agent-request-card').exists()).toBe(true);
    w.unmount();
  });
});

describe('Ф41-5: ручки нет, синхронизация', () => {
  it('404 на список заявок — молча, без ошибок в полосе', async () => {
    await store.loadAgentRequests();
    expect(store.state.agentRequests.items).toEqual({});
    expect(store.state.agentRequests.error).toBeNull();
    expect(store.state.syncError).toBeNull();
  });

  it('ждущие заявки из ответа списка попадают в стор', async () => {
    listAnswer = () => json({ requests: [move()] });
    await store.loadAgentRequests();
    expect(store.state.agentRequests.items[11]?.kind).toBe('move_channel');
  });
});

describe('Ф41-5: подписи автопилота', () => {
  it('прежняя заявка с auto: true — «принято автопилотом»', () => {
    const leave = {
      id: 1, account_id: 1, project_id: 3, channel_id: 5, title: 'Чат', chat_kind: 'group', reason: null, origin: 'session',
      status: 'approved', result: null, error: null, client_name: null, created_ts: NOW, decided_ts: NOW, done_ts: null,
      returned_ts: null, updated_ts: NOW, can_decline: false, leaving: true, seq: 1, auto: true,
    } as LeaveRequest;
    const w = mount(LeaveCard, { props: { leave } });
    expect(w.get('.auto-accepted').text()).toBe('принято автопилотом');
    const manual = mount(LeaveCard, { props: { leave: { ...leave, auto: false } } });
    expect(manual.find('.auto-accepted').exists()).toBe(false);
  });

  it('сообщение, ушедшее веткой выключателя, подписано «агент: <роль> · автопилот»', () => {
    const message = {
      id: 1, seq: 1, first_seq: 1, channel_id: 283, project_id: 3, ext_id: '1', thread_ext_id: null, reply_to_ext_id: null,
      grouped_id: null, is_me: true, kind: 'text', text: 'ответ', entities_json: null, attachments_json: null,
      forward_json: null, mentions_me: false, refs_json: null, reactions_json: null, ts: 1000, edited_ts: null,
      deleted: false, outbox_id: 5, outbox_status: 'sent', sender: null, outbox_auto_role: 'маркетолог', outbox_auto_session: 7,
    };
    const w = mount(MessageRow, { props: { message: message as never } });
    expect(w.get('.auto-role-mark').text()).toBe('агент: маркетолог · автопилот');
    const list = mount(MessageRow, { props: { message: { ...message, outbox_auto_session: null } as never } });
    expect(list.get('.auto-role-mark').text()).toBe('агент: маркетолог');
  });
});
