// Ф17-1b (`docs/architecture.md`, тесты F10): заявки сессий на вступление в группы и каналы
// Telegram — карточка (кнопки зовут approve/decline, «вступаю…», причина простыми словами, «заявка у админов»,
// предпросмотр — не HTML), применение `/sync.joins` и кадра SSE `join`, секция «Вступить?» и «Ждёт», переключатель
// режима в «Раскладке» шлёт `PATCH` `session_join`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { JoinRequest, Project, StatusResponse } from '../src/api/types';
import JoinCard from '../src/components/JoinCard.vue';
import MappingPanel from '../src/components/MappingPanel.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createJoinState } from '../src/store/joins';
import { createStaleness } from '../src/store/staleness';
import { connectStream, type EventSourceLike } from '../src/store/stream';
import { createStore } from '../src/store/store';
import { buildBoard } from '../src/util/board';
import { failReason, joinKindLine, joinTitle, visibleJoins } from '../src/util/joins';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 8, 30, 15, 0, 0).getTime();
const DAY = 24 * 60 * 60 * 1000;
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function join(overrides: Partial<JoinRequest> = {}): JoinRequest {
  return {
    id: 1,
    account_id: 1,
    project_id: 3,
    target_kind: 'username',
    target: 'sales_chat',
    reason: 'клиенты спрашивают про оплату',
    status: 'pending',
    auto: false,
    peek: {
      title: 'Продажи',
      kind: 'broadcast',
      participants: 12400,
      about: 'Обсуждаем продажи',
      request_needed: true,
      member: false,
      forum: false,
      ext_id: '-100123',
    },
    ext_id: '-100123',
    title: 'Продажи',
    channel_id: null,
    error: null,
    client_name: 'маркетолог',
    created_ts: NOW - 60_000,
    decided_ts: null,
    done_ts: null,
    updated_ts: NOW - 60_000,
    can_decline: true,
    joining: false,
    seq: 10,
    ...overrides,
  };
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, ...overrides };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function fakeFetch(handler: (s: Sent) => Response | undefined = () => undefined): {
  sent: Sent[];
  fetchMock: ReturnType<typeof vi.fn>;
} {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: url.pathname,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    sent.push(s);
    const custom = handler(s);
    if (custom !== undefined) return custom;
    if (s.path === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (s.path === '/api/v1/status') return jsonResponse({ accounts: [] });
    if (s.path === '/api/v1/projects' && s.method === 'GET') return jsonResponse([project()]);
    if (s.method === 'GET') return jsonResponse([]);
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.joins = createJoinState();
  store.state.projects = [project()];
  store.state.homeOpen = true;
  store.state.currentProjectId = null;
  store.state.currentChannelId = null;
  delete (window as unknown as { __xss?: number }).__xss;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('Ф17-1b: util/joins', () => {
  it('название, строка «канал · N подписчиков» / «группа · N участников», причины простыми словами', () => {
    expect(joinTitle(join())).toBe('Продажи');
    expect(joinTitle(join({ peek: null, title: null }))).toBe('@sales_chat');
    expect(joinTitle(join({ peek: null, title: null, target_kind: 'invite', target: 'AbC' }))).toBe('по приглашению');
    expect(joinKindLine(join())).toBe('канал · 12 400 подписчиков');
    expect(joinKindLine(join({ peek: { ...join().peek!, kind: 'group', participants: 85 } }))).toBe('группа · 85 участников');
    expect(joinKindLine(join({ peek: { ...join().peek!, kind: 'megagroup', participants: 3 } }))).toBe('группа · 3 участника');
    expect(joinKindLine(join({ peek: { ...join().peek!, kind: 'megagroup', forum: true, participants: 21 } }))).toBe(
      'форум · 21 участник',
    );
    expect(failReason('not_found')).toBe('такого канала или группы нет');
    expect(failReason('paid')).toBe('платный канал');
    expect(failReason('not_channel')).toBe('это не канал и не группа');
    expect(failReason('gave_up')).toBe('Telegram не отвечает, попробуйте позже');
    expect(failReason('FloodWaitError')).toBe('FloodWaitError');
  });

  it('видны: ждущие и одобренные; failed/requested — сутки после решения; joined, declined, checking — нет', () => {
    const list = [
      join({ id: 1, status: 'pending' }),
      join({ id: 2, status: 'approved' }),
      join({ id: 3, status: 'failed', decided_ts: NOW - DAY + 1000 }),
      join({ id: 4, status: 'failed', decided_ts: NOW - DAY - 1000 }),
      join({ id: 5, status: 'requested', decided_ts: NOW - 1000 }),
      join({ id: 6, status: 'failed', decided_ts: null }),
      join({ id: 7, status: 'joined', decided_ts: NOW }),
      join({ id: 8, status: 'declined', decided_ts: NOW }),
      join({ id: 9, status: 'checking' }),
    ];
    expect(visibleJoins(list, NOW).map((j) => j.id)).toEqual([1, 2, 3, 5]);
  });
});

describe('Ф17-1b: карточка', () => {
  it('показывает предпросмотр, «Зачем» и от кого; «Вступить» и «Отказать» зовут approve / decline', async () => {
    const { sent, fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(JoinCard, { props: { join: join() }, attachTo: document.body });
    const text = wrapper.text();
    expect(text).toContain('Продажи');
    expect(text).toContain('канал · 12 400 подписчиков');
    expect(text).toContain('нужно одобрение админов');
    expect(text).toContain('Обсуждаем продажи');
    expect(text).toContain('Зачем: клиенты спрашивают про оплату · маркетолог');
    await wrapper.get('button.join-approve').trigger('click');
    await flushPromises();
    await wrapper.get('button.join-decline').trigger('click');
    await flushPromises();
    const posts = sent.filter((s) => s.method === 'POST').map((s) => s.path);
    expect(posts).toContain('/api/v1/joins/1/approve');
    expect(posts).toContain('/api/v1/joins/1/decline');
    wrapper.unmount();
  });

  it('«вступаю…» без «Вступить»; «Отказать» — только пока can_decline', () => {
    const joining = mount(JoinCard, { props: { join: join({ status: 'approved', joining: true, can_decline: false }) } });
    expect(joining.text()).toContain('вступаю…');
    expect(joining.find('button.join-approve').exists()).toBe(false);
    expect(joining.find('button.join-decline').exists()).toBe(false);
    const early = mount(JoinCard, { props: { join: join({ status: 'approved', can_decline: true }) } });
    expect(early.find('button.join-approve').exists()).toBe(false);
    expect(early.find('button.join-decline').exists()).toBe(true);
    const noDecline = mount(JoinCard, { props: { join: join({ can_decline: false }) } });
    expect(noDecline.find('button.join-approve').exists()).toBe(true);
    expect(noDecline.find('button.join-decline').exists()).toBe(false);
  });

  it('исход: failed — причина простыми словами, requested — «заявка у админов», без кнопок', () => {
    const failed = mount(JoinCard, {
      props: { join: join({ status: 'failed', error: 'invite_expired', decided_ts: NOW, can_decline: false }) },
    });
    expect(failed.text()).toContain('приглашение истекло');
    expect(failed.find('button').exists()).toBe(false);
    const requested = mount(JoinCard, {
      props: { join: join({ status: 'requested', decided_ts: NOW, can_decline: false }) },
    });
    expect(requested.text()).toContain('заявка у админов');
    expect(requested.find('button').exists()).toBe(false);
    // решённая заявка — одна строка: без описания и «Зачем»
    expect(requested.classes()).toContain('join-card--line');
    expect(requested.find('.join-about').exists()).toBe(false);
    expect(requested.find('.join-reason').exists()).toBe(false);
  });

  it('409 при решении — заявка перечитывается, показана строка', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/joins/1/approve' ? jsonResponse({ detail: 'уже' }, 409) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(JoinCard, { props: { join: join() } });
    await wrapper.get('button.join-approve').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/joins')).toBe(true);
    expect(wrapper.get('.join-error').text()).toContain('уже изменилась');
  });

  it('название, описание и причина сессии — только текстом, не HTML', () => {
    const wrapper = mount(JoinCard, {
      props: {
        join: join({
          reason: XSS,
          client_name: XSS,
          peek: { ...join().peek!, title: XSS, about: XSS },
        }),
      },
      attachTo: document.body,
    });
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.find('script').exists()).toBe(false);
    expect(wrapper.text()).toContain('<img src=x');
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
    wrapper.unmount();
  });
});

describe('Ф17-1b: стор — /sync и кадр SSE', () => {
  it('/sync.joins попадает в стор; старшая по seq побеждает; закрытая заявка перестаёт быть видимой', async () => {
    let calls = 0;
    const { fetchMock } = fakeFetch((s) => {
      if (s.path !== '/api/v1/sync') return undefined;
      calls += 1;
      if (calls === 1) {
        return jsonResponse({ messages: [], channels: [], max_seq: 11, more: false, joins: [join({ seq: 11 })] });
      }
      if (calls === 2) {
        return jsonResponse({
          messages: [],
          channels: [],
          max_seq: 12,
          more: false,
          joins: [join({ seq: 9, status: 'declined' }), join({ id: 2, seq: 12 })],
        });
      }
      return jsonResponse({
        messages: [],
        channels: [],
        max_seq: 13,
        more: false,
        joins: [join({ seq: 13, status: 'joined', decided_ts: NOW })],
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.runSync();
    expect(s.state.joins.items[1]!.status).toBe('pending');
    await s.runSync();
    expect(s.state.joins.items[1]!.status).toBe('pending'); // seq 9 старше 11 — не откатывает
    expect(s.state.joins.items[2]).toBeDefined();
    await s.runSync();
    expect(s.state.joins.items[1]!.status).toBe('joined');
    expect(visibleJoins(Object.values(s.state.joins.items), NOW).map((j) => j.id)).toEqual([2]);
  });

  it('кадр SSE join запускает /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      readyState: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      close: () => {},
    };
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('join')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
  });

  it('loadJoins читает GET /joins', async () => {
    const { sent, fetchMock } = fakeFetch((s) => (s.path === '/api/v1/joins' ? jsonResponse([join()]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.loadJoins();
    expect(sent.some((x) => x.path === '/api/v1/joins')).toBe(true);
    expect(s.state.joins.items[1]!.id).toBe(1);
  });
});

describe('Ф17-1b: главная и «Ждёт»', () => {
  const HOME = { react: [], drafts: [], runs: [], sent: [] };
  const WORK = { tasks: {}, pending: {}, stale: {} };

  it('доска: заявка — в столбце своего проекта, ждущая входит в счётчик столбца', () => {
    const board = buildBoard(
      [project()],
      { ...HOME, joins: [join({ id: 1 }), join({ id: 2, status: 'joined', decided_ts: NOW })] },
      NOW,
    );
    expect(board.columns[0]!.joins.map((j) => j.id)).toEqual([1]);
    expect(board.columns[0]!.waitCount).toBe(1);
  });

  it('заявка «Вступить?» — карточкой в «Нужна реакция» столбца проекта (Ф20-4: отдельной секции нет)', () => {
    const board = buildBoard([project()], { ...HOME, joins: [join()] }, NOW);
    const wrapper = mount(ProjectColumn, { props: { column: board.columns[0]! } });
    expect(wrapper.get('[data-section="react"] .board-section-title').text()).toBe('Нужна реакция · 1');
    expect(wrapper.find('[data-section="react"] [data-section="joins"] .join-card').exists()).toBe(true);
    expect(wrapper.find('button.join-approve').exists()).toBe(true);
  });

  it('«Ждёт»: только заявки, ждущие решения владельца; «заявка у админов», «вступаю», неудача — не там', () => {
    const input = {
      projects: [project()],
      home: HOME,
      
      nowMs: NOW,
      joins: [
        join({ id: 1 }),
        join({ id: 2, status: 'requested', decided_ts: NOW - 1000, can_decline: false }),
        join({ id: 3, status: 'approved', decided_ts: NOW - 1000 }),
        join({ id: 4, status: 'failed', error: 'private', decided_ts: NOW - 1000, can_decline: false }),
      ],
    };
    const waiting = buildWaiting(input).flatMap((i) => (i.kind === 'join' ? [i.join.id] : []));
    expect(waiting).toEqual([1]);
    expect(homeDecisionCount(input)).toBe(1);
  });

  it('WaitList рисует карточку заявки', async () => {
    const { fetchMock } = fakeFetch((s) => (s.path === '/api/v1/joins' ? jsonResponse([join()]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    store.state.joins.items[1] = join();
    const wrapper = mount(WaitList);
    await flushPromises();
    expect(wrapper.find('.wait-item--join .join-card').exists()).toBe(true);
    wrapper.unmount();
  });
});

describe('Ф17-1b: «Раскладка» — режим вступления', () => {
  it('переключатель шлёт PATCH session_join', async () => {
    store.state.projects = [project({ session_join: 'confirm' })];
    store.state.status = { accounts: [] } as unknown as StatusResponse;
    const { sent, fetchMock } = fakeFetch((s) => (s.path === '/api/v1/map-rules' ? jsonResponse([]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const select = wrapper.get('.mapping-project-join-mode');
    expect((select.element as HTMLSelectElement).value).toBe('confirm');
    expect(wrapper.text()).toContain('сразу — только для подписанной сессии проекта');
    await select.setValue('auto');
    await flushPromises();
    const patch = sent.find((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/3');
    expect(patch?.body).toEqual({ session_join: 'auto' });
    wrapper.unmount();
  });
});
