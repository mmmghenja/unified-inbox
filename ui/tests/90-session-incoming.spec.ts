// Ф41-4 (`docs/architecture.md` + П4, П9, П11, П15, П16, интерфейс): подписка сессии в форме и в
// окне (`want_sub`, `sub`, `sub_stale`, `listening`, `autosend_channels`), «Поднять» при `wait_reason == "heavy"`, «Сжать»,
// события `signal` и `compact` в ленте, «контекст: уточнится после хода», «ждёт тебя» для тяжёлых сессий.
// Ответы ядра подделаны строго по контракту спеки.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, SessionEvent, SessionSub, SessionUsage } from '../src/api/types';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import SessionCacheLine from '../src/components/SessionCacheLine.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { buildBoard } from '../src/util/board';
import { startOfDay } from '../src/util/home';
import { SESSION_WAIT_LABEL, sessionWaitCount } from '../src/util/sessions';
import { stepView, usageLabel } from '../src/util/sessionSteps';
import { buildWantSub, emptySubForm, subLine } from '../src/util/sessionSub';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 10, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  params: URLSearchParams;
  body: unknown;
}

type Handler = (method: string, path: string, body: unknown) => Response | undefined;

function stubFetch(handler: Handler = () => undefined, autosend = 0): { sent: Sent[] } {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      sent.push({ method, path: url.pathname, params: url.searchParams, body });
      const custom = handler(method, url.pathname, body);
      if (custom !== undefined) return custom;
      if (url.pathname === '/api/v1/sessions/autosend-count') return json({ count: autosend });
      if (/\/events$/.test(url.pathname)) return json([]);
      if (/^\/api\/v1\/sessions\/\d+$/.test(url.pathname) && method === 'GET') {
        return json({ session: store.state.sessions.items[5] ?? session(), requests: [], says: [] });
      }
      if (url.pathname === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      if (url.pathname === '/api/v1/sessions' && method === 'GET') return json({ items: Object.values(store.state.sessions.items) });
      return json({});
    }),
  );
  return { sent };
}

function calls(sent: Sent[], method: string, path: string): Sent[] {
  return sent.filter((r) => r.method === method && r.path === path);
}

function sub(overrides: Partial<SessionSub> = {}): SessionSub {
  return {
    id: 7,
    mode: 'messages',
    msg_filter: 'all',
    keywords: [],
    exclude: [],
    batch_min: null,
    accounts: null,
    pending: 0,
    ...overrides,
  };
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5,
    project_id: 3,
    role: 'маркетолог',
    role_no: 1,
    engine: 'claude',
    model: 'sonnet',
    effort: null,
    mode: 'manual',
    title: null,
    state: 'idle',
    host_seen_ts: NOW,
    created_ts: NOW - 1000,
    updated_ts: NOW - 500,
    closed_ts: null,
    close_reason: null,
    created_by: 'ui',
    seq: 10,
    last_event_id: 20,
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
    workdir: '/home/owner/proj',
    cache: null,
    usage: null,
    want_sub: null,
    sub: null,
    sub_stale: false,
    autosend_channels: 0,
    ...overrides,
  };
}

function project(): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true };
}

function ev(overrides: Partial<SessionEvent>): SessionEvent {
  return { id: 1, ts: NOW, kind: 'status', tool: null, summary: '', is_error: false, ref: null, ...overrides };
}

function mountWindow(overrides: Partial<HubSession> = {}, events: SessionEvent[] = []) {
  const s = session(overrides);
  store.state.sessions.items[s.id] = s;
  store.state.sessions.events[s.id] = events;
  return mount(SessionWindow, { props: { session: s } });
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sessions = createSessionsState();
  store.state.projects = [project()];
  store.state.homeOpen = true;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --- форма новой сессии -------------------------------------------------------------------------------------

describe('Ф41-4: форма новой сессии — блок «Подписка»', () => {
  async function submit(w: ReturnType<typeof mount>): Promise<void> {
    await w.get('form').trigger('submit');
    await flushPromises();
  }

  it('умолчания: без роли — «не подписывать»; с ролью — «только сообщения агентов»; ручной выбор роль не перебивает', async () => {
    stubFetch();
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    const kind = w.get('.session-sub-kind');
    expect((kind.element as HTMLSelectElement).value).toBe('none');
    await w.get('.session-new-role').setValue('тестер');
    expect((kind.element as HTMLSelectElement).value).toBe('direct');
    await kind.setValue('cards');
    await w.get('.session-new-role').setValue('тестер 2');
    expect((kind.element as HTMLSelectElement).value).toBe('cards');
    w.unmount();
  });

  it('без роли и без выбора want_sub не уходит; режимы cards и direct — только mode', async () => {
    const { sent } = stubFetch((m, p) => (m === 'POST' && p === '/api/v1/sessions' ? json({ session: session({ id: 8 }) }) : undefined));
    const none = mount(NewSessionForm, { props: { projectId: 3 } });
    await submit(none);
    expect(calls(sent, 'POST', '/api/v1/sessions')[0]!.body).toEqual({ project_id: 3, engine: 'claude' });
    const cards = mount(NewSessionForm, { props: { projectId: 3 } });
    await cards.get('.session-sub-kind').setValue('cards');
    await submit(cards);
    expect(calls(sent, 'POST', '/api/v1/sessions')[1]!.body).toMatchObject({ want_sub: { mode: 'cards' } });
    expect((calls(sent, 'POST', '/api/v1/sessions')[1]!.body as { want_sub: object }).want_sub).toEqual({ mode: 'cards' });
    const direct = mount(NewSessionForm, { props: { projectId: 3 } });
    await direct.get('.session-new-role').setValue('тестер');
    await submit(direct);
    expect((calls(sent, 'POST', '/api/v1/sessions')[2]!.body as { want_sub: object }).want_sub).toEqual({ mode: 'direct' });
  });

  it('первое задание уходит репликой после создания; пустое — реплики нет; реплика не ушла — форма остаётся с пояснением', async () => {
    const created = (m: string, p: string): Response | undefined =>
      m === 'POST' && p === '/api/v1/sessions' ? json({ session: session({ id: 8 }) }) : undefined;
    const { sent } = stubFetch(created);
    const empty = mount(NewSessionForm, { props: { projectId: 3 } });
    await submit(empty);
    expect(sent.filter((c) => c.method === 'POST' && /\/sessions\/8\//.test(c.path))).toHaveLength(0);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-first').setValue('  прогони тесты  ');
    await submit(w);
    const says = sent.filter((c) => c.method === 'POST' && /\/sessions\/8\//.test(c.path));
    expect(says).toHaveLength(1);
    expect(says[0]!.body).toMatchObject({ text: 'прогони тесты' });
    expect(w.emitted('done')).toHaveLength(1);
    vi.unstubAllGlobals();
    const failing = stubFetch((m, p) => created(m, p) ?? (m === 'POST' && /\/sessions\/8\//.test(p) ? json({ error: 'x' }, 500) : undefined));
    const bad = mount(NewSessionForm, { props: { projectId: 3 } });
    await bad.get('.session-new-first').setValue('задание');
    await submit(bad);
    expect(bad.emitted('done')).toBeUndefined();
    expect(bad.get('.work-error').text()).toContain('задание не ушло');
    await submit(bad);
    expect(bad.emitted('done')).toHaveLength(1);
    expect(calls(failing.sent, 'POST', '/api/v1/sessions')).toHaveLength(1);
  });

  it('«сообщения»: личное, ключи, исключения, пачка, аккаунты «только» и «все, кроме»', async () => {
    const { sent } = stubFetch((m, p) => (m === 'POST' && p === '/api/v1/sessions' ? json({ session: session({ id: 8 }) }) : undefined));
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-sub-kind').setValue('messages');
    await w.get('.session-sub-personal').setValue(true);
    await w.get('.session-sub-keywords').setValue('цена, договор ,');
    await w.get('.session-sub-exclude').setValue('спам');
    await w.get('.session-sub-batch').setValue('30');
    await w.get('.session-sub-accounts-mode').setValue('only');
    await w.get('.session-sub-accounts').setValue('1, 8');
    await submit(w);
    expect((calls(sent, 'POST', '/api/v1/sessions')[0]!.body as { want_sub: unknown }).want_sub).toEqual({
      mode: 'messages',
      msg_filter: 'personal',
      keywords: ['цена', 'договор'],
      exclude: ['спам'],
      batch_min: 30,
      accounts: { only: [1, 8] },
    });
    const w2 = mount(NewSessionForm, { props: { projectId: 3 } });
    await w2.get('.session-sub-kind').setValue('messages');
    await w2.get('.session-sub-accounts-mode').setValue('except');
    await w2.get('.session-sub-accounts').setValue('8');
    await submit(w2);
    expect((calls(sent, 'POST', '/api/v1/sessions')[1]!.body as { want_sub: unknown }).want_sub).toEqual({
      mode: 'messages',
      msg_filter: 'all',
      keywords: [],
      exclude: [],
      batch_min: null,
      accounts: { except: [8] },
    });
  });

  it('лёгкие проверки: пачка 0–120 целое, аккаунты — целые; запрос не уходит', async () => {
    const { sent } = stubFetch();
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-sub-kind').setValue('messages');
    await w.get('.session-sub-batch').setValue('121');
    await submit(w);
    expect(w.get('[role="alert"]').text()).toMatch(/Пачка/);
    await w.get('.session-sub-batch').setValue('0');
    await w.get('.session-sub-accounts-mode').setValue('only');
    await w.get('.session-sub-accounts').setValue('1, x');
    await submit(w);
    expect(w.get('[role="alert"]').text()).toMatch(/Аккаунты/);
    expect(calls(sent, 'POST', '/api/v1/sessions')).toHaveLength(0);
    expect(buildWantSub({ ...emptySubForm('messages'), batch: '0' })).toMatchObject({ ok: true });
    expect(buildWantSub({ ...emptySubForm('messages'), batch: '1.5' })).toMatchObject({ ok: false });
  });

  it('отказ ядра 422 — его строкой', async () => {
    stubFetch((m, p) => (m === 'POST' && p === '/api/v1/sessions' ? json({ detail: 'want_sub: подписки проекта выключены' }, 422) : undefined));
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-sub-kind').setValue('cards');
    await submit(w);
    expect(w.get('[role="alert"]').text()).toBe('want_sub: подписки проекта выключены');
  });

  it('роль с беседами «без подтверждения» — строка про это; без роли и при нуле — нет', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const { sent } = stubFetch(() => undefined, 3);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    expect(w.find('.session-sub-autosend').exists()).toBe(false);
    await w.get('.session-new-role').setValue('маркетолог');
    vi.advanceTimersByTime(300);
    await flushPromises();
    const asked = calls(sent, 'GET', '/api/v1/sessions/autosend-count');
    expect(asked).toHaveLength(1);
    expect(asked[0]!.params.get('project_id')).toBe('3');
    expect(asked[0]!.params.get('role')).toBe('маркетолог');
    expect(w.get('.session-sub-autosend').text()).toBe('у роли 3 бесед без подтверждения: сессия будет отвечать в них сама');
    await w.get('.session-new-role').setValue('');
    expect(w.find('.session-sub-autosend').exists()).toBe(false);
    w.unmount();
  });

  it('число 0 — строки нет', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    stubFetch(() => undefined, 0);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    await w.get('.session-new-role').setValue('тестер');
    vi.advanceTimersByTime(300);
    await flushPromises();
    expect(w.find('.session-sub-autosend').exists()).toBe(false);
    w.unmount();
  });
});

// --- строка подписки в окне ---------------------------------------------------------------------------------

describe('Ф41-4: строка подписки в окне сессии', () => {
  it('все состояния: не подписана / подпишется / слушает / хост не отвечает / настройки изменены', async () => {
    stubFetch();
    const none = mountWindow({});
    expect(none.get('.session-sub-line').text()).toContain('не подписана');
    none.unmount();

    const pending = mountWindow({ want_sub: { mode: 'direct' } });
    expect(pending.get('.session-sub-line').text()).toContain('подпишется при первом подъёме');
    pending.unmount();

    const live = mountWindow({
      want_sub: { mode: 'messages' },
      sub: sub({ id: 9, keywords: ['цена', 'договор'], pending: 2 }),
      listening: true,
    });
    const text = live.get('.session-sub-line').text();
    expect(text).toContain('подписка #9 · сообщения · ключи: цена, договор · ждёт сигналов: 2');
    expect(text).toContain('слушает');
    expect(live.find('.session-sub-stale').exists()).toBe(false);
    live.unmount();

    const silent = mountWindow({ sub: sub(), listening: false, sub_stale: true });
    expect(silent.get('.session-sub-line').text()).toContain('хост не отвечает');
    expect(silent.get('.session-sub-stale').text()).toBe('настройки изменены — применятся после сна, можно усыпить');
    silent.unmount();
    await flushPromises();
  });

  it('число бесед «без подтверждения» у роли — в строке подписки', () => {
    expect(subLine(session({ autosend_channels: 2 })).autosend).toBe('у роли 2 бесед без подтверждения: сессия будет отвечать в них сама');
    expect(subLine(session({ autosend_channels: 0 })).autosend).toBeNull();
  });

  it('«подписка ▾» открывает поля с текущим want_sub; «Сохранить» — PATCH /sessions/{id}', async () => {
    const { sent } = stubFetch((m, p, body) => {
      if (m === 'PATCH' && p === '/api/v1/sessions/5') {
        return json({ session: session({ seq: 11, want_sub: (body as { want_sub: HubSession['want_sub'] }).want_sub }) });
      }
      return undefined;
    });
    const w = mountWindow({ want_sub: { mode: 'messages', msg_filter: 'personal', keywords: ['цена'], accounts: { except: [8] } } });
    await flushPromises();
    expect(w.find('.session-sub-edit').exists()).toBe(false);
    await w.get('.session-sub-toggle').trigger('click');
    expect((w.get('.session-sub-kind').element as HTMLSelectElement).value).toBe('messages');
    expect((w.get('.session-sub-keywords').element as HTMLInputElement).value).toBe('цена');
    expect((w.get('.session-sub-accounts-mode').element as HTMLSelectElement).value).toBe('except');
    expect(w.get('.session-sub-edit').text()).toContain('применится при следующем подъёме сессии');
    await w.get('.session-sub-keywords').setValue('цена, договор');
    await w.get('.session-sub-save').trigger('click');
    await flushPromises();
    const patch = calls(sent, 'PATCH', '/api/v1/sessions/5');
    expect(patch).toHaveLength(1);
    expect(patch[0]!.body).toEqual({
      want_sub: {
        mode: 'messages',
        msg_filter: 'personal',
        keywords: ['цена', 'договор'],
        exclude: [],
        batch_min: null,
        accounts: { except: [8] },
      },
    });
    expect(w.find('.session-sub-edit').exists()).toBe(false);
    w.unmount();
  });

  it('«не подписывать» шлёт want_sub: null; отказ ядра — его строкой, поля остаются', async () => {
    const { sent } = stubFetch((m, p) =>
      m === 'PATCH' && p === '/api/v1/sessions/5' ? json({ detail: 'сессия хаба закрыта' }, 409) : undefined,
    );
    const w = mountWindow({ want_sub: { mode: 'cards' } });
    await flushPromises();
    await w.get('.session-sub-toggle').trigger('click');
    await w.get('.session-sub-kind').setValue('none');
    await w.get('.session-sub-save').trigger('click');
    await flushPromises();
    expect(calls(sent, 'PATCH', '/api/v1/sessions/5')[0]!.body).toEqual({ want_sub: null });
    expect(w.get('.work-error').text()).toBe('сессия хаба закрыта');
    expect(w.find('.session-sub-edit').exists()).toBe(true);
    w.unmount();
  });

  it('неверная пачка в правке — до запроса', async () => {
    const { sent } = stubFetch();
    const w = mountWindow({ want_sub: { mode: 'messages' } });
    await flushPromises();
    await w.get('.session-sub-toggle').trigger('click');
    await w.get('.session-sub-batch').setValue('abc');
    await w.get('.session-sub-save').trigger('click');
    await flushPromises();
    expect(calls(sent, 'PATCH', '/api/v1/sessions/5')).toHaveLength(0);
    expect(w.get('.work-error').text()).toMatch(/Пачка/);
    w.unmount();
  });
});

// --- «Поднять» и «Сжать» ------------------------------------------------------------------------------------

describe('Ф41-4: тяжёлая сессия и сжатие', () => {
  it('wait_reason heavy: «K сигналов ждут: …» и «Поднять» -> POST /wake; без heavy строки нет', async () => {
    const { sent } = stubFetch();
    const w = mountWindow({ state: 'asleep', wait_reason: 'heavy', sub: sub({ pending: 3 }) });
    await flushPromises();
    expect(w.get('.session-sub-heavy').text()).toContain('3 сигналов ждут: контекст большой, кэш остыл');
    await w.get('.session-wake').trigger('click');
    await flushPromises();
    expect(calls(sent, 'POST', '/api/v1/sessions/5/wake')).toHaveLength(1);
    w.unmount();
    const other = mountWindow({ state: 'asleep', wait_reason: 'slot', sub: sub({ pending: 3 }) });
    expect(other.find('.session-sub-heavy').exists()).toBe(false);
    other.unmount();
    await flushPromises();
  });

  it('«Поднять»: 409 «поднимать не на что» показывается как есть', async () => {
    stubFetch((m, p) => (m === 'POST' && p === '/api/v1/sessions/5/wake' ? json({ detail: 'поднимать не на что' }, 409) : undefined));
    const w = mountWindow({ state: 'asleep', wait_reason: 'heavy', sub: sub({ pending: 1 }) });
    await flushPromises();
    await w.get('.session-wake').trigger('click');
    await flushPromises();
    expect(w.get('.work-error').text()).toBe('поднимать не на что');
    w.unmount();
  });

  it('«Сжать» рядом с «Усыпить»: активна только при state idle; клик -> POST /compact', async () => {
    const { sent } = stubFetch();
    const idle = mountWindow({ state: 'idle' });
    await flushPromises();
    const buttons = idle.findAll('.work-controls button').map((b) => b.text());
    expect(buttons.indexOf('Сжать')).toBe(buttons.indexOf('Усыпить') + 1);
    expect(idle.get('.session-compact').attributes('disabled')).toBeUndefined();
    await idle.get('.session-compact').trigger('click');
    await flushPromises();
    expect(calls(sent, 'POST', '/api/v1/sessions/5/compact')).toHaveLength(1);
    idle.unmount();
    for (const state of ['running', 'asleep'] as const) {
      const w = mountWindow({ state });
      expect(w.get('.session-compact').attributes('disabled')).toBeDefined();
      w.unmount();
    }
    await flushPromises();
  });
});

describe('Ф41-4 П19: пауза лимита подписки', () => {
  it('кнопка «Отправить всё равно» — только при paused с ждущим; зовёт /wake', async () => {
    const { sent } = stubFetch();
    const w = mountWindow({ state: 'asleep', wait_reason: 'paused', queued_says: 1 });
    await flushPromises();
    expect(w.get('.session-paused').text()).toContain('пауза лимита подписки: реплика ждёт');
    await w.get('.session-wake-anyway').trigger('click');
    await flushPromises();
    expect(calls(sent, 'POST', '/api/v1/sessions/5/wake')).toHaveLength(1);
    w.unmount();
    const bySub = mountWindow({ wait_reason: 'paused', sub: sub({ pending: 2 }) });
    expect(bySub.find('.session-wake-anyway').exists()).toBe(true);
    bySub.unmount();
    const empty = mountWindow({ wait_reason: 'paused' });
    expect(empty.find('.session-wake-anyway').exists()).toBe(false);
    empty.unmount();
    const other = mountWindow({ wait_reason: 'slot', queued_says: 3 });
    expect(other.find('.session-wake-anyway').exists()).toBe(false);
    other.unmount();
    await flushPromises();
  });

  it('409 показан текстом из detail; подпись paused', async () => {
    stubFetch((m, p) => (m === 'POST' && p === '/api/v1/sessions/5/wake' ? json({ detail: 'поднимать не на что' }, 409) : undefined));
    const w = mountWindow({ wait_reason: 'paused', queued_says: 1 });
    await flushPromises();
    await w.get('.session-wake-anyway').trigger('click');
    await flushPromises();
    expect(w.get('.work-error').text()).toBe('поднимать не на что');
    expect(SESSION_WAIT_LABEL.paused).toBe('пауза лимита подписки');
    w.unmount();
  });
});

// --- лента и контекст ---------------------------------------------------------------------------------------

describe('Ф41-4: события ленты и строка контекста', () => {
  it('status/signal: только ненулевые части; compact: «контекст сжат: было 152 тыс.»', async () => {
    stubFetch();
    const w = mountWindow({}, [
      ev({
        id: 1,
        summary: 'сигнал хаба: карточек 1, сообщений агентов 2',
        brief: { type: 'signal', cards: 1, messages: 0, reminders: 0, agents: 2, from_user: 0, drafts: 0, channels: 0 },
      }),
      ev({ id: 2, kind: 'compact', summary: 'контекст сжат', brief: { type: 'compact', before: 152_000 } }),
    ]);
    await flushPromises();
    expect(w.get('[data-event-id="1"]').text()).toBe('сигнал хаба: карточек 1, сообщений агентов 2');
    expect(w.get('[data-event-id="2"]').text()).toBe('контекст сжат: было 152 тыс.');
    w.unmount();
    const all = stepView(
      ev({ brief: { type: 'signal', cards: 1, messages: 2, reminders: 3, agents: 4, from_user: 5, drafts: 6, channels: 7 } }),
    );
    expect(all.text).toBe(
      'сигнал хаба: карточек 1, сообщений 2, напоминаний 3, сообщений агентов 4, от владельца 5, исходов черновиков 6, бесед 7',
    );
    const old = stepView(ev({ summary: 'сессия усыплена' }));
    expect(old.text).toBe('сессия усыплена'); // без brief — как раньше
  });

  it('usage.context_tokens == null: «контекст: уточнится после хода», цена остаётся', () => {
    const usage: SessionUsage = { context_tokens: null, context_limit: 200_000, cost_usd: 1.2, updated_ts: NOW };
    expect(usageLabel(usage)).toEqual({ context: 'контекст: уточнится после хода', contextTone: 'normal', cost: 'всего $1.20' });
    const w = mount(SessionCacheLine, { props: { usage, nowMs: NOW } });
    expect(w.text()).toBe('контекст: уточнится после хода · всего $1.20');
  });
});

// --- «ждёт тебя» --------------------------------------------------------------------------------------------

describe('Ф41-4: тяжёлая сессия в «Ждёт» и счётчиках', () => {
  const input = () => ({ projects: store.state.projects, home: { react: [], drafts: [] }, sessions: store.state.sessions });

  it('heavy + ждущие сигналы — элемент «ждёт»; счётчик вкладки и проекта его считают; без pending или без heavy — нет', () => {
    store.state.sessions.items = {
      5: session({ wait_reason: 'heavy', sub: sub({ pending: 3 }), state: 'asleep' }),
      6: session({ id: 6, wait_reason: 'heavy', sub: sub({ pending: 0 }) }),
      7: session({ id: 7, wait_reason: 'slot', sub: sub({ pending: 4 }) }),
      8: session({ id: 8, wait_reason: 'heavy', sub: sub({ pending: 2 }), closed_ts: NOW }),
    };
    const items = buildWaiting(input());
    expect(items.map((i) => i.kind)).toEqual(['session_heavy']);
    expect(homeDecisionCount(input())).toBe(1);
    expect(sessionWaitCount(store.state.sessions, 3)).toBe(1);
    const board = buildBoard(store.state.projects, store.state.home, NOW, store.state.sessions);
    const col = board.columns.find((c) => c.kind === 'project');
    expect(col?.waitCount).toBe(1);
    expect(col?.state).toMatchObject({ wait: true });
  });

  it('вкладка «Ждёт»: «сессия <роль>: K сигналов ждут подъёма» и «Поднять» -> POST /wake', async () => {
    const { sent } = stubFetch();
    store.state.sessions.items = { 5: session({ wait_reason: 'heavy', sub: sub({ pending: 3 }), state: 'asleep' }) };
    const w = mount(WaitList);
    await flushPromises();
    expect(w.get('.wait-session-title').text()).toBe('сессия маркетолог: 3 сигналов ждут подъёма');
    await w.get('.wait-session-wake').trigger('click');
    await flushPromises();
    expect(calls(sent, 'POST', '/api/v1/sessions/5/wake')).toHaveLength(1);
    w.unmount();
  });

  it('подпись причины ожидания heavy есть', () => {
    expect(SESSION_WAIT_LABEL.heavy).toMatch(/контекст большой/);
  });
});
