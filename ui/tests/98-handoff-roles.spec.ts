// Ф41-7/8 (интерфейс, `docs/architecture.md`, 2.3, 3.2): «Модель и усилие…», «Новая с передачей
// дел…» (форма, замена столбца, кнопка у `heavy`, строка «дела переданы сессии #N»), список ролей в форме «Новая сессия».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, RoleInfo } from '../src/api/types';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import WaitList from '../src/components/WaitList.vue';
import { boardState, openColumn, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { resetRoles } from '../src/store/roles';
import { createSessionsState } from '../src/store/sessions';

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
let roles: () => Response = () => json({ detail: 'not found' }, 404);

function stubFetch(): void {
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      sent.push({ method, path: url.pathname, body });
      const p = url.pathname;
      if (p === '/api/v1/roles') return roles();
      if (p === '/api/v1/sessions' && method === 'GET') return json({ items: Object.values(store.state.sessions.items) });
      if (p === '/api/v1/sessions/5/handoff') {
        return json({ session: session({ id: 9, created_by: 'handoff', engine: body.engine ?? 'claude' }), closed: session({ closed_ts: NOW, close_reason: 'handoff' }) });
      }
      if (/^\/api\/v1\/sessions\/\d+$/.test(p) && method === 'PATCH') return json({ session: session({ model: body.model ?? null, effort: body.effort ?? null, seq: 99 }) });
      if (/^\/api\/v1\/sessions\/\d+$/.test(p)) return json({ session: session(), requests: [], says: [] });
      if (/\/events$/.test(p)) return json([]);
      return json([]);
    }),
  );
}

function project(): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true } as Project;
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5,
    project_id: 3,
    role: 'программист',
    role_no: 1,
    engine: 'claude',
    model: 'opus',
    effort: null,
    mode: 'manual',
    title: null,
    state: 'asleep',
    host_seen_ts: NOW,
    created_ts: NOW,
    updated_ts: NOW,
    closed_ts: null,
    close_reason: null,
    created_by: 'ui',
    seq: 10,
    last_event_id: null,
    last_turn_ts: null,
    failure: null,
    wait_reason: null,
    pending_requests: 0,
    queued_says: 0,
    interrupt_requested: false,
    sleep_requested: false,
    sub_id: null,
    listening: false,
    engine_session_id: null,
    workdir: '/p',
    cache: null,
    usage: { context_tokens: 184000 } as never,
    ...overrides,
  } as HubSession;
}

const ROLES: RoleInfo[] = [
  { key: 'programmer', title: 'Программист', names: ['программист', 'разработчик'], sub: 'direct' },
  { key: 'orchestrator', title: 'Оркестратор', names: ['оркестратор'], sub: 'project' },
];

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetRoles();
  roles = () => json({ detail: 'not found' }, 404);
  stubFetch();
  store.state.sessions = createSessionsState();
  store.state.handoffTo = {};
  store.state.projects = [project()];
  store.state.syncError = null;
  store.state.sessions.items = { 5: session() };
});

afterEach(() => {
  resetBoard();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-7а: «Модель и усилие…»', () => {
  it('«⋯» -> форма с предупреждением про кэш и размером контекста; PATCH {model, effort}', async () => {
    const w = mount(SessionWindow, { props: { session: session() } });
    await flushPromises();
    await w.get('.session-more-toggle').trigger('click');
    await w.get('.session-more-model').trigger('click');
    expect(w.get('.session-model-warning').text()).toContain('кэш сбросится');
    expect(w.get('.session-model-warning').text()).toContain('сейчас 184 тыс.');
    expect(w.get('.session-model-warning').text()).toContain('применится со следующего входа');
    // усилие есть у обоих движков; у Claude Code — свои уровни
    expect(w.findAll('.session-effort-input option').map((o) => o.attributes('value'))).toEqual(['', 'low', 'medium', 'high', 'xhigh', 'max']);
    await w.get('.session-model-input').setValue('sonnet');
    sent.length = 0;
    await w.get('form.session-model-form').trigger('submit');
    await flushPromises();
    const patch = sent.find((s) => s.method === 'PATCH');
    expect(patch?.path).toBe('/api/v1/sessions/5');
    expect(patch?.body).toEqual({ model: 'sonnet', effort: null });
    expect(w.find('form.session-model-form').exists()).toBe(false);
    w.unmount();
  });

  it('усилие Claude Code уходит в PATCH; значение не из списка (старые данные) остаётся пунктом', async () => {
    const w = mount(SessionWindow, { props: { session: session({ effort: 'weird' }) } });
    await flushPromises();
    await w.get('.session-more-toggle').trigger('click');
    await w.get('.session-more-model').trigger('click');
    const opts = w.findAll('.session-effort-input option').map((o) => o.attributes('value'));
    expect(opts).toContain('weird');
    await w.get('.session-effort-input').setValue('xhigh');
    sent.length = 0;
    await w.get('form.session-model-form').trigger('submit');
    await flushPromises();
    expect(sent.find((s) => s.method === 'PATCH')?.body).toEqual({ model: 'opus', effort: 'xhigh' });
    w.unmount();
  });

  it('чип «модель · усилие» в шапке открывает форму', async () => {
    const w = mount(SessionWindow, { props: { session: session({ model: 'opus', effort: 'high' }) } });
    await flushPromises();
    const chip = w.get('.session-model-chip');
    expect(chip.text()).toBe('opus · high');
    expect(chip.attributes('title')).toBe('Модель и усилие');
    await chip.trigger('click');
    expect(w.find('form.session-model-form').exists()).toBe(true);
    w.unmount();
  });

  it('пустая модель уходит как null (умолчание движка); усилие у OpenCode уходит', async () => {
    const oc = session({ engine: 'opencode', model: 'x', effort: 'high' });
    const w = mount(SessionWindow, { props: { session: oc } });
    await flushPromises();
    await w.get('.session-more-toggle').trigger('click');
    await w.get('.session-more-model').trigger('click');
    expect((w.get('.session-effort-input').element as HTMLSelectElement).value).toBe('high');
    await w.get('.session-model-input').setValue('');
    sent.length = 0;
    await w.get('form.session-model-form').trigger('submit');
    await flushPromises();
    expect(sent.find((s) => s.method === 'PATCH')?.body).toEqual({ model: null, effort: 'high' });
    w.unmount();
  });
});

describe('Ф41-7б: «Новая с передачей дел…»', () => {
  it('форма: поля от прежней сессии, пояснение; POST handoff; столбец заменён на том же месте', async () => {
    openColumn({ kind: 'owner_channel' });
    openColumn({ kind: 'session', id: 5 });
    openColumn({ kind: 'agent', key: 'p3:x:1' });
    const w = mount(SessionWindow, { props: { session: session() } });
    await flushPromises();
    await w.get('.session-more-toggle').trigger('click');
    await w.get('.session-more-handoff').trigger('click');
    expect(w.get('.session-handoff-note').text()).toContain('прежняя сессия закроется');
    expect((w.get('.session-handoff-engine').element as HTMLSelectElement).value).toBe('claude');
    expect((w.get('.session-handoff-model').element as HTMLInputElement).value).toBe('opus');
    await w.get('.session-handoff-mode').setValue('auto');
    sent.length = 0;
    await w.get('form.session-handoff-form').trigger('submit');
    await flushPromises();
    const post = sent.find((s) => s.path === '/api/v1/sessions/5/handoff');
    expect(post?.method).toBe('POST');
    expect(post?.body).toEqual({ engine: 'claude', mode: 'auto', model: 'opus', effort: null });
    expect(boardState.layout.open).toEqual([{ kind: 'owner_channel' }, { kind: 'session', id: 9 }, { kind: 'agent', key: 'p3:x:1' }]);
    expect(store.state.handoffTo[5]).toBe(9);
    expect(store.state.sessions.items[9]).toBeDefined();
    w.unmount();
  });

  it('у сессии с причиной ожидания heavy — «Новая с передачей дел» рядом с «Поднять»', async () => {
    const heavy = session({ wait_reason: 'heavy', sub: { id: 1, mode: 'messages', msg_filter: 'all', keywords: [], exclude: [], batch_min: null, accounts: null, pending: 2 } as never });
    store.state.sessions.items = { 5: heavy };
    const w = mount(SessionWindow, { props: { session: heavy } });
    await flushPromises();
    expect(w.find('.session-wake').exists()).toBe(true);
    await w.get('.session-handoff-heavy').trigger('click');
    expect(w.find('form.session-handoff-form').exists()).toBe(true);
    w.unmount();
  });

  it('карточка «Ждёт тебя» с heavy: «Новая с передачей дел» шлёт handoff с параметрами сессии', async () => {
    const heavy = session({ wait_reason: 'heavy', sub: { id: 1, mode: 'messages', msg_filter: 'all', keywords: [], exclude: [], batch_min: null, accounts: null, pending: 2 } as never });
    store.state.sessions.items = { 5: heavy };
    const w = mount(WaitList);
    await flushPromises();
    sent.length = 0;
    await w.get('.wait-session-handoff').trigger('click');
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/sessions/5/handoff')?.body).toEqual({ engine: 'claude', model: 'opus', effort: null, mode: 'manual' });
    w.unmount();
  });

  it('закрытая с close_reason handoff — последней строкой «дела переданы сессии #N»; без номера — без номера', async () => {
    store.state.handoffTo = { 5: 9 };
    const closed = session({ closed_ts: NOW, close_reason: 'handoff' });
    let w = mount(SessionWindow, { props: { session: closed } });
    expect(w.get('.session-handoff-line').text()).toBe('дела переданы сессии #9');
    w.unmount();
    store.state.handoffTo = {};
    w = mount(SessionWindow, { props: { session: closed } });
    expect(w.get('.session-handoff-line').text()).toBe('дела переданы сессии');
    w.unmount();
    w = mount(SessionWindow, { props: { session: session() } });
    expect(w.find('.session-handoff-line').exists()).toBe(false);
    w.unmount();
  });

  it('отказ ядра при передаче — текстом в форме, столбец на месте', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'потолок 8 сессий на проект' }, 409)));
    openColumn({ kind: 'session', id: 5 });
    const w = mount(SessionWindow, { props: { session: session() } });
    await w.get('.session-more-toggle').trigger('click');
    await w.get('.session-more-handoff').trigger('click');
    await w.get('form.session-handoff-form').trigger('submit');
    await flushPromises();
    expect(w.text()).toContain('потолок 8 сессий на проект');
    expect(boardState.layout.open).toEqual([{ kind: 'session', id: 5 }]);
    w.unmount();
  });
});

describe('Ф41-8: роли в форме «Новая сессия»', () => {
  it('ручки /roles нет (404) — списка нет, остаётся свободный текст', async () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await flushPromises();
    expect(w.find('.session-new-role-pick').exists()).toBe(false);
    expect(w.find('.session-new-role').exists()).toBe(true);
    w.unmount();
  });

  it('выбор роли подставляет первое имя и подписку по умолчанию (direct / project)', async () => {
    roles = () => json(ROLES);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await flushPromises();
    await w.get('.session-new-role-pick').setValue('programmer');
    expect((w.get('.session-new-role').element as HTMLInputElement).value).toBe('программист');
    const kinds = () => [(w.get('.session-sub-kind').element as HTMLSelectElement).value];
    expect(kinds()).toContain('direct');
    await w.get('.session-new-role-pick').setValue('orchestrator');
    expect((w.get('.session-new-role').element as HTMLInputElement).value).toBe('оркестратор');
    expect(kinds()).toContain('cards');
    // свой текст поверх списка остаётся возможным
    await w.get('.session-new-role').setValue('мой агент');
    sent.length = 0;
    await w.get('form').trigger('submit');
    await flushPromises();
    const post = sent.find((s) => s.method === 'POST' && s.path === '/api/v1/sessions');
    expect((post?.body as { role: string }).role).toBe('мой агент');
    w.unmount();
  });
});
