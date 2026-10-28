// Ф50-7 (интерфейс, `docs/architecture.md`, Р9, П2, П5): подпапка сессии — поле формы, режим
// заявки, карточка заявки, передача дел, команда терминала, приписка в шапке столбца и в списке, строка в меню.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { AgentRequest, HubSession, Project } from '../src/api/types';
import AgentRequestCard from '../src/components/AgentRequestCard.vue';
import BoardSession from '../src/components/BoardSession.vue';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import SessionHandoffForm from '../src/components/SessionHandoffForm.vue';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { resetRoles } from '../src/store/roles';
import { createSessionsState } from '../src/store/sessions';
import { sessionRow } from '../src/util/boardList';
import { sessionTerminalCommand, subdirLabel } from '../src/util/sessions';

const NOW = new Date(2026, 9, 10, 12, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'исследователь', role_no: 1, engine: 'claude', model: 'opus', effort: null, mode: 'manual',
    title: null, state: 'asleep', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: 'abc',
    workdir: '/p', cache: null, usage: null, autosend_ok: false, autosend_channels: 0, role_file: null, subdir: null, ...overrides,
  } as HubSession;
}

function partner(overrides: Partial<AgentRequest> = {}): AgentRequest {
  return {
    id: 12, seq: 2, kind: 'new_session', project_id: 3, who: 'оркестратор', reason: null, status: 'pending', auto: false,
    note: null, created_ts: NOW, decided_ts: null,
    payload: { role: 'тестер', task: 'проверить', engine: 'claude', mode: 'manual', subdir: 'orders/zakaz-17', role_file: 'docs/ROLE.md' },
    ...overrides,
  } as AgentRequest;
}

interface Sent { method: string; path: string; body: Record<string, unknown> | undefined }
let sent: Sent[] = [];
let approveReply: () => Response;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetRoles();
  sent = [];
  approveReply = () => json({ request: { ...partner(), status: 'approved' } });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const p = url.pathname;
    sent.push({ method, path: p, body });
    if (p === '/api/v1/roles') return json([]);
    if (p === '/api/v1/sessions/autosend-count') return json({ count: 0 });
    if (p === '/api/v1/sessions' && method === 'POST') return json({ session: session({ id: 8 }) });
    if (/^\/api\/v1\/agent-requests\/\d+\/approve$/.test(p)) return approveReply();
    if (p === '/api/v1/sessions/5/handoff') return json({ session: session({ id: 9 }), closed: session({ closed_ts: NOW }) });
    if (/^\/api\/v1\/sessions\/\d+$/.test(p)) return json({ session: session(), requests: [], says: [] });
    if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
    return json([]);
  }));
  store.state.sessions = createSessionsState();
  store.state.projects = [{ id: 3, name: 'П', color: '#f00', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true } as Project];
  store.state.sessions.items = { 5: session() };
});

afterEach(() => {
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const created = () => sent.filter((s) => s.method === 'POST' && s.path === '/api/v1/sessions');
const approved = () => sent.filter((s) => s.path === '/api/v1/agent-requests/12/approve');

describe('Ф50-7: форма «Новая сессия»', () => {
  it('поле с подсказкой; подпапка уходит без пробелов', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    expect(w.text()).toContain('Папка в проекте (необязательно)');
    expect(w.get('.session-subdir-hint').text()).toBe('относительно папки проекта; после создания не меняется');
    await w.get('.session-new-subdir').setValue('  orders/zakaz-17 ');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(created()[0]!.body).toEqual({ project_id: 3, engine: 'claude', subdir: 'orders/zakaz-17' });
  });

  it('пустое поле — ключа нет', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('form').trigger('submit');
    await flushPromises();
    expect('subdir' in created()[0]!.body!).toBe(false);
  });
});

describe('Ф50-7: режим заявки в форме', () => {
  it('оба поля видны и предзаполнены из заявки; изменение уходит', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3, request: partner() } });
    expect((w.get('.session-new-subdir').element as HTMLInputElement).value).toBe('orders/zakaz-17');
    expect((w.get('.session-new-role-file').element as HTMLInputElement).value).toBe('docs/ROLE.md');
    await w.get('.session-new-subdir').setValue('other');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(approved()[0]!.body).toMatchObject({ subdir: 'other', role_file: 'docs/ROLE.md' });
  });

  it('очистка полей = пустые строки, ключи всё равно есть', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3, request: partner() } });
    await w.get('.session-new-subdir').setValue('');
    await w.get('.session-new-role-file').setValue('');
    await w.get('form').trigger('submit');
    await flushPromises();
    const body = approved()[0]!.body!;
    expect(body.subdir).toBe('');
    expect(body.role_file).toBe('');
  });

  it('заявка без папки и файла: ключи есть, значения пустые', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3, request: partner({ payload: { role: 'x', engine: 'claude' } }) } });
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(approved()[0]!.body).toMatchObject({ subdir: '', role_file: '' });
  });
});

describe('Ф50-7: карточка заявки', () => {
  it('строки папки и файла, предупреждения; простое «Завести» шлёт оба ключа из заявки', async () => {
    const w = mount(AgentRequestCard, {
      props: { request: partner({ subdir_warning: 'папки orders/zakaz-17 нет в проекте', role_file_warning: 'файла роли нет' }) },
    });
    expect(w.get('.agent-request-subdir').text()).toBe('папка: orders/zakaz-17');
    expect(w.get('.agent-request-role-file').text()).toBe('файл роли: docs/ROLE.md');
    expect(w.get('.agent-request-subdir-warning').text()).toBe('папки orders/zakaz-17 нет в проекте');
    expect(w.get('.agent-request-role-file-warning').text()).toBe('файла роли нет');
    await w.get('.agent-request-approve').trigger('click');
    await flushPromises();
    expect(approved()[0]!.body).toEqual({ subdir: 'orders/zakaz-17', role_file: 'docs/ROLE.md' });
  });

  it('без папки и файла строк нет; в теле пустые строки; чужой ввод не разбирается как разметка', async () => {
    const w = mount(AgentRequestCard, { props: { request: partner({ payload: { role: 'x' }, subdir_warning: '<b>x</b>' }) } });
    expect(w.find('.agent-request-subdir').exists()).toBe(false);
    expect(w.find('.agent-request-role-file').exists()).toBe(false);
    expect(w.get('.agent-request-subdir-warning').find('b').exists()).toBe(false);
    await w.get('.agent-request-approve').trigger('click');
    await flushPromises();
    expect(approved()[0]!.body).toEqual({ subdir: '', role_file: '' });
  });

  it('422 ядра: его текст в карточке', async () => {
    approveReply = () => json({ detail: 'папки orders нет в проекте' }, 422);
    const w = mount(AgentRequestCard, { props: { request: partner() } });
    await w.get('.agent-request-approve').trigger('click');
    await flushPromises();
    expect(w.get('.agent-request-error').text()).toContain('папки orders нет в проекте');
  });
});

describe('Ф50-7: передача дел', () => {
  it('папка прежней сессии подставлена; без изменений ключа нет', async () => {
    const w = mount(SessionHandoffForm, { props: { session: session({ subdir: 'a/b' }) } });
    expect((w.get('.session-handoff-subdir').element as HTMLInputElement).value).toBe('a/b');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect('subdir' in sent.find((s) => s.path === '/api/v1/sessions/5/handoff')!.body!).toBe(false);
  });

  it('изменение уходит строкой, очистка — пустой строкой', async () => {
    const w = mount(SessionHandoffForm, { props: { session: session({ subdir: 'a/b' }) } });
    await w.get('.session-handoff-subdir').setValue('c');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/sessions/5/handoff')!.body!.subdir).toBe('c');
    sent = [];
    const w2 = mount(SessionHandoffForm, { props: { session: session({ subdir: 'a/b' }) } });
    await w2.get('.session-handoff-subdir').setValue('');
    await w2.get('form').trigger('submit');
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/sessions/5/handoff')!.body!.subdir).toBe('');
  });
});

describe('Ф50-7: «Открыть в терминале»', () => {
  it('cwd, workdir + subdir, только workdir', () => {
    expect(sessionTerminalCommand(session({ cwd: '/p/orders/z' }))).toBe('cd /p/orders/z && claude --resume abc');
    expect(sessionTerminalCommand(session({ subdir: 'orders/z' }))).toBe('cd /p/orders/z && claude --resume abc');
    expect(sessionTerminalCommand(session())).toBe('cd /p && claude --resume abc');
  });
});

describe('Ф50-7: приписка подпапки', () => {
  it('длинная обрезается с начала', () => {
    expect(subdirLabel('orders/z')).toBe('orders/z');
    const label = subdirLabel('very/long/path/to/some/place/zakaz-17');
    expect(label.startsWith('…/')).toBe(true);
    expect(label.endsWith('zakaz-17')).toBe(true);
    expect(label.length).toBeLessThanOrEqual(26);
  });

  it('в строке списка слева: подпапка в записи строки', () => {
    expect(sessionRow(session({ subdir: 'orders/z' }), undefined, NOW).subdir).toBe('orders/z');
    expect(sessionRow(session(), undefined, NOW).subdir).toBeNull();
  });

  it('в шапке столбца: приписка с title полным путём; без подпапки нет', async () => {
    const full = 'very/long/path/to/some/place/zakaz-17';
    const w = mount(BoardSession, { props: { item: { kind: 'session', id: 5 }, session: session({ subdir: full }) } as never });
    // Ф90-1: в шапке столбца подпапки больше нет — строка «Папка: …» в меню «⋯» с title полным путём.
    expect(w.find('.board-col-subdir').exists()).toBe(false);
    await w.get('.board-col-menu').trigger('click');
    const el = w.get('.session-menu-subdir');
    expect(el.attributes('title')).toBe(full);
    expect(el.text()).toBe(`Папка: ${full}`);
    const w2 = mount(BoardSession, { props: { item: { kind: 'session', id: 5 }, session: session() } as never });
    await w2.get('.board-col-menu').trigger('click');
    expect(w2.find('.session-menu-subdir').exists()).toBe(false);
  });

  it('в меню «⋯»: строка «Папка: …» только при подпапке', () => {
    const w = mount(SessionMenuItems, { props: { win: null, session: session({ subdir: 'orders/z' }) } });
    expect(w.get('.session-menu-subdir').text()).toBe('Папка: orders/z');
    const w2 = mount(SessionMenuItems, { props: { win: null, session: session() } });
    expect(w2.find('.session-menu-subdir').exists()).toBe(false);
  });
});
