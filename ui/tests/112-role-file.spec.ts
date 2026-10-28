// Ф48-3 (`docs/architecture.md`): поле «Файл роли (путь в проекте)» в форме новой сессии и в
// форме «Роль…» (пустое = null), предупреждение ядра «файл не прочитать», окно «Инструкция роли» у сессии.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, SessionRoleText } from '../src/api/types';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import RoleTextModal from '../src/components/RoleTextModal.vue';
import SessionRoleForm from '../src/components/SessionRoleForm.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { resetRoles } from '../src/store/roles';
import { createSessionsState } from '../src/store/sessions';

const NOW = new Date(2026, 9, 9, 21, 0, 0).getTime();
const PATH = 'docs/marketing/researcher/ROLE.md';
const PIN_HINT =
  'Claude Code закрепляет инструкцию при создании сессии; правку файла сессия получит сообщением при следующем запуске, насовсем — в новой сессии. OpenCode читает файл при каждом запуске.';
const HINT = 'MD-файл с инструкцией роли; подставляется в сессию дословно и не теряется при сжатии';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'исследователь', role_no: 1, engine: 'claude', model: 'opus', effort: null, mode: 'manual',
    title: null, state: 'asleep', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, autosend_ok: false, autosend_channels: 0, role_file: null, ...overrides,
  } as HubSession;
}

interface Sent { method: string; path: string; body: Record<string, unknown> | undefined }
let sent: Sent[] = [];
let createReply: (body: Record<string, unknown>) => Response;
let patchReply: (body: Record<string, unknown>) => Response;
let roleText: () => Response;

function text(overrides: Partial<SessionRoleText> = {}): SessionRoleText {
  return { role: 'исследователь 1', common: 'Общий свод\nстрока 2', role_file: null, file_text: null, file_reason: null, ...overrides };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetRoles();
  sent = [];
  createReply = (b) => json({ session: session({ id: 8, role_file: (b.role_file as string | undefined) ?? null }) });
  patchReply = (b) => json({ session: session({ role_file: (b.role_file as string | null | undefined) ?? null, seq: 99 }) });
  roleText = () => json(text());
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const p = url.pathname;
    sent.push({ method, path: p, body });
    if (p === '/api/v1/roles') return json([]);
    if (p === '/api/v1/sessions/autosend-count') return json({ count: 0 });
    if (p === '/api/v1/sessions' && method === 'POST') return createReply(body);
    if (/^\/api\/v1\/sessions\/\d+\/role-text$/.test(p)) return roleText();
    if (/^\/api\/v1\/sessions\/\d+$/.test(p) && method === 'PATCH') return patchReply(body);
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
const patched = () => sent.filter((s) => s.method === 'PATCH');

describe('Ф48-3: «Новая сессия» — поле «Файл роли»', () => {
  it('поле с подсказкой; путь уходит без крайних пробелов', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    expect(w.text()).toContain('Файл роли (путь в проекте)');
    expect(w.get('.session-role-file-hint').text()).toContain(HINT);
    await w.get('.session-new-role-file').setValue(`  ${PATH} `);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(created()[0]!.body).toEqual({ project_id: 3, engine: 'claude', role_file: PATH });
    expect(w.emitted('done')).toHaveLength(1);
  });

  it('пустое поле — ключа в запросе нет (файла нет)', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-role-file').setValue('   ');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(created()[0]!.body).toEqual({ project_id: 3, engine: 'claude' });
    expect('role_file' in created()[0]!.body!).toBe(false);
  });

  it('предупреждение ядра: сессия заведена, строка показана, второе нажатие закрывает без второй сессии', async () => {
    createReply = () => json({ session: session({ id: 8, role_file: PATH }), role_file_warning: `файл роли ${PATH}: нет файла` });
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-role-file').setValue(PATH);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.emitted('done')).toBeUndefined();
    expect(w.get('.session-role-file-warning').text()).toContain(`файл роли ${PATH}: нет файла`);
    expect(w.get('.session-new-submit').text()).toBe('Закрыть');
    expect(store.state.sessions.items[8]?.role_file).toBe(PATH);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(created()).toHaveLength(1);
    expect(w.emitted('done')).toHaveLength(1);
  });

  it('отказ ядра 422 на негодный путь — его строкой', async () => {
    createReply = () => json({ detail: 'role_file: без «..» — файл лежит внутри рабочей папки проекта' }, 422);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-role-file').setValue('../ROLE.md');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.get('[role="alert"]').text()).toContain('без «..»');
    expect(w.emitted('done')).toBeUndefined();
  });
});

describe('Ф48-3: «Роль…» — поле «Файл роли»', () => {
  async function save(w: ReturnType<typeof mount>) {
    await w.get('form.session-role-form').trigger('submit');
    await flushPromises();
  }

  it('подпись про следующий запуск; задать файл: PATCH {role, role_file}', async () => {
    const w = mount(SessionRoleForm, { props: { session: session() } });
    expect(w.get('.session-role-file-hint').text()).toContain(HINT);
    expect(w.get('.session-role-file-hint').text()).toContain(PIN_HINT); // Ф48-5: подпись исправлена
    expect((w.get('.session-role-file').element as HTMLInputElement).value).toBe('');
    await w.get('.session-role-file').setValue(` ${PATH} `);
    await save(w);
    expect(patched()[0]!.body).toEqual({ role: 'исследователь', role_file: PATH });
    expect(w.emitted('done')).toHaveLength(1);
    expect(store.state.sessions.items[5]?.role_file).toBe(PATH);
  });

  it('поле заполнено текущим путём; очистить — role_file: null', async () => {
    const w = mount(SessionRoleForm, { props: { session: session({ role_file: PATH }) } });
    expect((w.get('.session-role-file').element as HTMLInputElement).value).toBe(PATH);
    await w.get('.session-role-file').setValue('  ');
    await save(w);
    expect(patched()[0]!.body).toEqual({ role: 'исследователь', role_file: null });
  });

  it('файл не менялся — ключа role_file нет; ничего не менялось — запроса нет', async () => {
    let w = mount(SessionRoleForm, { props: { session: session({ role_file: PATH }) } });
    await save(w);
    expect(patched()).toHaveLength(0);
    expect(w.emitted('cancel')).toHaveLength(1);
    w = mount(SessionRoleForm, { props: { session: session({ role_file: PATH }) } });
    await w.get('.session-role-input').setValue('тестер');
    await save(w);
    expect(patched()[0]!.body).toEqual({ role: 'тестер', autosend_ok: false });
  });

  it('предупреждение ядра: сохранено, форма остаётся со строкой, «Закрыть» закрывает без второго запроса', async () => {
    patchReply = () => json({ session: session({ role_file: PATH }), role_file_warning: `файл роли ${PATH}: нет файла` });
    const w = mount(SessionRoleForm, { props: { session: session() } });
    await w.get('.session-role-file').setValue(PATH);
    await save(w);
    expect(w.emitted('done')).toBeUndefined();
    expect(w.get('.session-role-file-warning').text()).toContain(`Сохранено. Но файл роли ${PATH}: нет файла`);
    expect(w.get('.session-role-save').text()).toBe('Закрыть');
    await save(w);
    expect(patched()).toHaveLength(1);
    expect(w.emitted('done')).toHaveLength(1);
  });

  it('422 ядра — строкой, форма остаётся', async () => {
    patchReply = () => json({ detail: 'role_file: нужен MD-файл (расширение .md)' }, 422);
    const w = mount(SessionRoleForm, { props: { session: session() } });
    await w.get('.session-role-file').setValue('ROLE.txt');
    await save(w);
    expect(w.get('.work-error').text()).toContain('нужен MD-файл');
    expect(w.emitted('done')).toBeUndefined();
  });
});

describe('Ф48-3: окно «Инструкция роли» у сессии', () => {
  async function open() {
    const w = mount(RoleTextModal, { props: { sessionId: 5, title: 'Исследователь' }, attachTo: document.body });
    await flushPromises();
    return w;
  }
  const q = (sel: string) => document.body.querySelector(sel);

  it('общий текст, путь файла и его содержимое — текстом, без разбора разметки', async () => {
    roleText = () => json(text({ role_file: PATH, file_text: '# Роль\n<b>жирный</b>\n  отступ' }));
    const w = await open();
    expect(sent.some((s) => s.path === '/api/v1/sessions/5/role-text')).toBe(true);
    const blocks = document.body.querySelectorAll('.role-text-content');
    expect(blocks[0]?.textContent).toBe('Общий свод\nстрока 2');
    expect(q('.role-text-file')?.textContent).toContain(`Файл роли: ${PATH}`);
    expect(q('.role-text-file-content')?.textContent).toBe('# Роль\n<b>жирный</b>\n  отступ');
    expect(q('.role-text-file-content b')).toBeNull();
    expect(q('.role-text-file-reason')).toBeNull();
    w.unmount();
  });

  it('файл не прочитан — причина строкой, содержимого нет', async () => {
    roleText = () => json(text({ role_file: 'out.md', file_reason: 'путь выходит за рабочую папку проекта' }));
    const w = await open();
    expect(q('.role-text-file')?.textContent).toContain('Файл роли: out.md');
    expect(q('.role-text-file-reason')?.textContent).toContain('Файл не прочитан: путь выходит за рабочую папку проекта');
    expect(q('.role-text-file-content')).toBeNull();
    w.unmount();
  });

  it('файла нет — только общий текст и «Файл роли не задан»', async () => {
    const w = await open();
    expect(document.body.querySelectorAll('.role-text-content')).toHaveLength(1);
    expect(q('.role-text-file--none')?.textContent).toContain('Файл роли не задан');
    expect(q('.role-text-file-reason')).toBeNull();
    w.unmount();
  });

  it('404 — сессии уже нет', async () => {
    roleText = () => json({ detail: 'not found' }, 404);
    const w = await open();
    expect(q('.work-error')?.textContent).toContain('Такой сессии уже нет');
    w.unmount();
  });
});
