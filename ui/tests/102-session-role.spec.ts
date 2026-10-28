// Смена роли существующей сессии («Роль…») и чтение инструкции роли окном поверх.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, RoleInfo } from '../src/api/types';
import RoleTextModal from '../src/components/RoleTextModal.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { resetRoles } from '../src/store/roles';
import { createSessionsState } from '../src/store/sessions';

const NOW = new Date(2026, 9, 9, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent { method: string; path: string; body: unknown }
let sent: Sent[] = [];
let patchReply: (body: Record<string, unknown>) => Response = () => json({});
let roleText: () => Response = () => json({ key: 'programmer', title: 'Программист', names: ['программист'], sub: 'direct', text: 'Общий свод\nстрока 2' });

const ROLES: RoleInfo[] = [
  { key: 'programmer', title: 'Программист', names: ['программист', 'разработчик'], sub: 'direct' },
  { key: 'tester', title: 'Тестер', names: ['тестер'], sub: 'direct' },
];

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: 'opus', effort: null, mode: 'manual',
    title: null, state: 'asleep', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, autosend_ok: true, autosend_channels: 2, ...overrides,
  } as HubSession;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetRoles();
  sent = [];
  patchReply = (b) => json({ session: session({ role: (b.role as string | null) ?? null, seq: 99 }) });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    sent.push({ method, path: url.pathname, body });
    const p = url.pathname;
    if (p === '/api/v1/roles') return json(ROLES);
    if (p.startsWith('/api/v1/roles/')) return roleText();
    if (p === '/api/v1/sessions/autosend-count') return json({ count: 3 });
    if (/^\/api\/v1\/sessions\/\d+\/role-text$/.test(p)) {
      return json({ role: null, common: 'Общий свод', role_file: null, file_text: null, file_reason: null });
    }
    if (/^\/api\/v1\/sessions\/\d+$/.test(p) && method === 'PATCH') return patchReply(body);
    if (/^\/api\/v1\/sessions\/\d+$/.test(p)) return json({ session: session(), requests: [], says: [] });
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

async function openForm(s: HubSession = session()) {
  const w = mount(SessionWindow, { props: { session: s }, attachTo: document.body });
  await flushPromises();
  await w.get('.session-more-toggle').trigger('click');
  await w.get('.session-more-role').trigger('click');
  return w;
}

async function submit(w: Awaited<ReturnType<typeof openForm>>) {
  sent.length = 0;
  await w.get('form.session-role-form').trigger('submit');
  await flushPromises();
  return sent.find((s) => s.method === 'PATCH');
}

describe('«Роль…»', () => {
  it('смена роли: role + autosend_ok=false (имя изменилось — галка сброшена)', async () => {
    const w = await openForm();
    expect((w.get('.session-role-autosend').element as HTMLInputElement).checked).toBe(true);
    await w.get('.session-role-input').setValue('тестер 2');
    expect((w.get('.session-role-autosend').element as HTMLInputElement).checked).toBe(false);
    expect(w.get('.session-model-warning').text()).toContain('Применится со следующего входа');
    const patch = await submit(w);
    expect(patch?.path).toBe('/api/v1/sessions/5');
    expect(patch?.body).toEqual({ role: 'тестер 2', autosend_ok: false });
    expect(w.find('form.session-role-form').exists()).toBe(false);
    w.unmount();
  });

  it('галку можно поставить после смены имени; подсказка числа бесед', async () => {
    const w = await openForm();
    await w.get('.session-role-input').setValue('тестер');
    await w.get('.session-role-autosend').setValue(true);
    await new Promise((r) => setTimeout(r, 300));
    await flushPromises();
    expect(w.get('form.session-role-form .session-sub-autosend').text()).toContain('у роли 3 бесед');
    expect((await submit(w))?.body).toEqual({ role: 'тестер', autosend_ok: true });
    w.unmount();
  });

  it('снять роль: role=null', async () => {
    const w = await openForm();
    await w.get('.session-role-input').setValue('  ');
    expect((await submit(w))?.body).toEqual({ role: null, autosend_ok: false });
    w.unmount();
  });

  it('с номером', async () => {
    const w = await openForm();
    await w.get('.session-role-input').setValue('тестер');
    await w.get('.session-role-number').setValue('2');
    expect((await submit(w))?.body).toEqual({ role: 'тестер', number: 2, autosend_ok: false });
    w.unmount();
  });

  it('ничего не изменено: запроса нет, форма закрыта', async () => {
    const w = await openForm();
    expect(await submit(w)).toBeUndefined();
    expect(w.find('form.session-role-form').exists()).toBe(false);
    w.unmount();
  });

  it('только галка: PATCH {autosend_ok}', async () => {
    const w = await openForm();
    await w.get('.session-role-autosend').setValue(false);
    expect((await submit(w))?.body).toEqual({ autosend_ok: false });
    w.unmount();
  });

  it('ошибка 409 показана как есть, форма остаётся', async () => {
    patchReply = () => json({ detail: 'Линия занята' , message: 'Линия занята' }, 409);
    const w = await openForm();
    await w.get('.session-role-input').setValue('тестер');
    await submit(w);
    expect(w.get('.work-error').text()).toContain('Линия занята');
    expect(w.find('form.session-role-form').exists()).toBe(true);
    w.unmount();
  });

  it('новая роль сразу в сторе и в шапке', async () => {
    const w = await openForm();
    await w.get('.session-role-input').setValue('тестер');
    await submit(w);
    expect(store.state.sessions.items[5]?.role).toBe('тестер');
    w.unmount();
  });
});

describe('«Инструкция роли»', () => {
  async function menu(role: string | null) {
    const w = mount(SessionWindow, { props: { session: session({ role }) }, attachTo: document.body });
    await flushPromises();
    await w.get('.session-more-toggle').trigger('click');
    return w;
  }

  // Ф48-3: окно читает запись сессии (`/sessions/{id}/role-text`), а не каталог; заголовок — по каталогу, иначе по роли.
  it('роль из каталога без учёта регистра: заголовок каталога, запрос — по сессии', async () => {
    const w = await menu('Разработчик');
    expect(w.find('.session-more-role-text').exists()).toBe(true);
    await w.get('.session-more-role-text').trigger('click');
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"][aria-label="Инструкция роли: Программист"]')).not.toBeNull();
    expect(sent.some((s) => s.path === '/api/v1/sessions/5/role-text')).toBe(true);
    expect(sent.some((s) => s.path.startsWith('/api/v1/roles/'))).toBe(false);
    w.unmount();
  });

  it('пункт есть и у роли не из каталога, и у сессии без роли (Ф48-3: общий текст и файл роли)', async () => {
    let w = await menu('самодельная');
    await w.get('.session-more-role-text').trigger('click');
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"][aria-label="Инструкция роли: самодельная"]')).not.toBeNull();
    expect(document.body.querySelector('.role-text-file--none')?.textContent).toContain('Файл роли не задан');
    w.unmount();
    document.body.innerHTML = '';
    w = await menu(null);
    await w.get('.session-more-role-text').trigger('click');
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"][aria-label="Инструкция роли: Сессия #5"]')).not.toBeNull();
    w.unmount();
  });
});

describe('RoleTextModal', () => {
  it('загрузка, затем текст с переносами', async () => {
    const w = mount(RoleTextModal, { props: { roleKey: 'programmer', title: 'Программист' }, attachTo: document.body });
    expect(document.body.textContent).toContain('загружается…');
    await flushPromises();
    const pre = document.body.querySelector('.role-text-content');
    expect(pre?.textContent).toBe('Общий свод\nстрока 2');
    expect(document.body.querySelector('[aria-modal="true"]')).not.toBeNull();
    w.unmount();
  });

  it('404 — строка ошибки', async () => {
    roleText = () => json({ detail: 'нет' }, 404);
    const w = mount(RoleTextModal, { props: { roleKey: 'x', title: 'Х' }, attachTo: document.body });
    await flushPromises();
    expect(document.body.querySelector('.work-error')?.textContent).toContain('Такой роли нет');
    w.unmount();
  });

  it('Esc закрывает', async () => {
    const w = mount(RoleTextModal, { props: { roleKey: 'programmer', title: 'П' }, attachTo: document.body });
    await flushPromises();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(w.emitted('close')).toHaveLength(1);
    w.unmount();
  });

  it('разметка в тексте не исполняется', async () => {
    roleText = () => json({ key: 'k', title: 'К', names: ['к'], sub: 'direct', text: '<script>alert(1)</script><b>x</b>' });
    const w = mount(RoleTextModal, { props: { roleKey: 'k', title: 'К' }, attachTo: document.body });
    await flushPromises();
    const pre = document.body.querySelector('.role-text-content');
    expect(pre?.textContent).toBe('<script>alert(1)</script><b>x</b>');
    expect(pre?.querySelector('script, b')).toBeNull();
    w.unmount();
  });
});
