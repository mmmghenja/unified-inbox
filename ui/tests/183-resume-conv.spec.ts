// Ф99-1 (`docs/architecture.md`, П2, П3): «Продолжить разговор» в форме новой сессии — список прошлых
// разговоров Claude Code папки проекта, по умолчанию «новый разговор»; поле только у Claude Code и без подпапки;
// подсказка ядра «разговор менялся только что» держит форму открытой.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, ProjectConversation } from '../src/api/types';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import { store } from '../src/store/instance';
import { resetRoles } from '../src/store/roles';
import { createSessionsState } from '../src/store/sessions';
import { conversationLabel } from '../src/util/sessions';

const NOW = new Date(2026, 9, 28, 12, 0, 0).getTime();
const FREE = '11111111-2222-3333-4444-555555555555';
const BUSY = '66666666-7777-8888-9999-000000000000';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function conv(overrides: Partial<ProjectConversation> = {}): ProjectConversation {
  return {
    id: FREE, last_ts: new Date(2026, 9, 27, 9, 5).getTime(), size: 2048, preview: 'Привет, разберись с дачей',
    hub_session_id: null, ...overrides,
  };
}

interface Sent { method: string; path: string; body: Record<string, unknown> | undefined }
let sent: Sent[] = [];
let createReply: () => Response;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetRoles();
  sent = [];
  createReply = () => json({ session: { id: 8, project_id: 3 } as HubSession });
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const p = url.pathname;
    sent.push({ method, path: p, body });
    if (p === '/api/v1/roles') return json([]);
    if (p === '/api/v1/sessions/autosend-count') return json({ count: 0 });
    if (p === '/api/v1/projects/3/conversations') {
      return json({ items: [conv(), conv({ id: BUSY, preview: '', hub_session_id: 7 })], workdir: '/p' });
    }
    if (p === '/api/v1/sessions' && method === 'POST') return createReply();
    if (/^\/api\/v1\/sessions\/\d+$/.test(p)) return json({ session: { id: 8 }, requests: [], says: [] });
    if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
    return json([]);
  }));
  store.state.sessions = createSessionsState();
  store.state.projects = [{ id: 3, name: 'П', color: '#f00', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true } as Project];
});

afterEach(() => {
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const created = () => sent.filter((s) => s.method === 'POST' && s.path === '/api/v1/sessions');
const listed = () => sent.filter((s) => s.path === '/api/v1/projects/3/conversations');

describe('Ф99-1: подпись разговора', () => {
  it('время, начало реплики, размер; занятый — с номером сессии', () => {
    expect(conversationLabel(conv(), NOW)).toBe('27.10 09:05 · «Привет, разберись с дачей» · 2 КиБ');
    expect(conversationLabel(conv({ preview: '', hub_session_id: 7 }), NOW)).toBe(
      '27.10 09:05 · без реплик · 2 КиБ — ведёт сессия #7',
    );
    const long = conversationLabel(conv({ preview: 'а'.repeat(100) }), NOW);
    expect(long).toContain(`«${'а'.repeat(69)}…»`);
    expect(conversationLabel(conv({ last_ts: new Date(2025, 0, 2, 3, 4).getTime() }), NOW)).toMatch(/^02\.01\.2025 03:04/);
  });
});

describe('Ф99-1: форма «Новая сессия»', () => {
  it('по умолчанию — новый разговор, ключа нет; занятый разговор не выбрать', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await flushPromises();
    const options = w.findAll('.session-new-resume option');
    expect(options.map((o) => o.text())).toEqual([
      'новый разговор',
      conversationLabel(conv()),
      conversationLabel(conv({ id: BUSY, preview: '', hub_session_id: 7 })),
    ]);
    expect((options[2]!.element as HTMLOptionElement).disabled).toBe(true);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect('resume_conv' in created()[0]!.body!).toBe(false);
  });

  it('выбранный разговор уходит полем resume_conv', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await flushPromises();
    await w.get('.session-new-resume').setValue(FREE);
    expect(w.find('.session-resume-hint').exists()).toBe(true);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(created()[0]!.body).toEqual({ project_id: 3, engine: 'claude', resume_conv: FREE });
  });

  it('с подпапкой поля нет и выбор не уходит; без подпапки список не грузится заново', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await flushPromises();
    await w.get('.session-new-resume').setValue(FREE);
    await w.get('.session-new-subdir').setValue('orders');
    expect(w.find('.session-new-resume').exists()).toBe(false);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(created()[0]!.body).toEqual({ project_id: 3, engine: 'claude', subdir: 'orders' });
    await w.get('.session-new-subdir').setValue('');
    await flushPromises();
    expect((w.get('.session-new-resume').element as HTMLSelectElement).value).toBe('');
    expect(listed()).toHaveLength(1);
  });

  it('подсказка ядра «менялся только что» — форма остаётся, второе нажатие закрывает', async () => {
    createReply = () => json({ session: { id: 8, project_id: 3 } as HubSession, resume_warning: 'разговор менялся меньше 2 минут назад' });
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await flushPromises();
    await w.get('.session-new-resume').setValue(FREE);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.get('.session-resume-warning').text()).toBe('Сессия заведена. Но разговор менялся меньше 2 минут назад.');
    expect(w.emitted('done')).toBeUndefined();
    expect(w.get('.session-new-submit').text()).toBe('Закрыть');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.emitted('done')).toHaveLength(1);
    expect(created()).toHaveLength(1);
  });

  it('список не загрузился — строка, форма работает', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      const p = new URL(input, 'http://localhost').pathname;
      sent.push({ method: init?.method ?? 'GET', path: p, body: undefined });
      if (p === '/api/v1/projects/3/conversations') return json({ detail: 'нет' }, 500);
      return json([]);
    }));
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await flushPromises();
    expect(w.get('.session-resume-error').text()).toBe('Список разговоров не загрузился.');
  });
});
