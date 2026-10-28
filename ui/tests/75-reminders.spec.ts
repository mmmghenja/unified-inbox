// Ф34-2 (`docs/architecture.md`: напоминания по беседам — `/sync.reminders` и кадр SSE,
// строка на главной и её кнопки («Отложить» шлёт верный `at`), строки бесед рядом со строками-напоминаниями
// (`last: null`) не ломаются, значок и список в шапке беседы, постановка и показ ошибок 422/429.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, HomeReactItem, Project, Reminder } from '../src/api/types';
import ChannelReminders from '../src/components/ChannelReminders.vue';
import ChannelView from '../src/components/ChannelView.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import ReminderCard from '../src/components/ReminderCard.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createReminderState, applyReminder } from '../src/store/reminders';
import { createStaleness } from '../src/store/staleness';
import { connectStream, type EventSourceLike } from '../src/store/stream';
import { createStore } from '../src/store/store';
import { buildBoard } from '../src/util/board';
import {
  badgeLabel,
  firedReminders,
  fromLocalInput,
  pendingOfChannel,
  toLocalInput,
  tomorrowTen,
  whoLabel,
} from '../src/util/reminders';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 9, 3, 15, 0, 0).getTime();
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function reminder(overrides: Partial<Reminder> = {}): Reminder {
  return {
    id: 1,
    channel_id: 7,
    project_id: 3,
    due_ts: NOW - 60_000,
    note: 'уточнить сроки',
    cancel_if_reply: true,
    status: 'fired',
    by: 'session',
    role: 'маркетолог',
    created_ts: NOW - 3_600_000,
    updated_ts: NOW - 60_000,
    fired_ts: NOW - 60_000,
    delivered_ts: null,
    closed_ts: null,
    end_reason: null,
    waiting: true,
    seq: 10,
    channel: { id: 7, title: 'Иван Петров', project_id: 3, archived: false },
    ...overrides,
  };
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, ...overrides };
}

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: 7,
    ext_id: 'tg:7',
    kind: 'dm',
    title: 'Иван Петров',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 3,
    attention: 'react',
    ...overrides,
  } as Channel;
}

function reactItem(overrides: Partial<HomeReactItem> = {}): HomeReactItem {
  return {
    channel: channel({ id: 9, title: 'Обычная беседа' }),
    project: { id: 3, name: 'Umbrella', color: null },
    last: { sender: 'Аня', excerpt: 'привет', ts: NOW - 1000, message_id: 5 },
    card: null,
    draft_count: 0,
    reminder: null,
    ...overrides,
  };
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
      query: url.search,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    sent.push(s);
    const custom = handler(s);
    if (custom !== undefined) return custom;
    if (s.path === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (s.path === '/api/v1/status') return jsonResponse({ accounts: [] });
    if (s.path === '/api/v1/reminders' && s.method === 'GET') return jsonResponse({ items: [] });
    if (s.method === 'GET') return jsonResponse([]);
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.reminders = createReminderState();
  store.state.projects = [project()];
  store.state.sync.channelsById = { 7: channel() };
  store.state.homeOpen = true;
  store.state.currentProjectId = null;
  store.state.currentChannelId = null;
  store.state.home.react = [];
  delete (window as unknown as { __xss?: number }).__xss;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('Ф34-2: util/reminders', () => {
  it('сработавшие и ждущие отбираются, кто поставил — «я» / роль / «сессия»', () => {
    const list = [
      reminder({ id: 1, status: 'fired', due_ts: 200 }),
      reminder({ id: 2, status: 'pending', due_ts: 500 }),
      reminder({ id: 3, status: 'fired', due_ts: 100 }),
      reminder({ id: 4, status: 'done' }),
      reminder({ id: 5, status: 'pending', channel_id: 8, due_ts: 50 }),
    ];
    expect(firedReminders(list).map((r) => r.id)).toEqual([3, 1]);
    expect(pendingOfChannel(list, 7).map((r) => r.id)).toEqual([2]);
    expect(whoLabel(reminder({ by: 'me', role: null }))).toBe('я');
    expect(whoLabel(reminder({ role: 'маркетолог' }))).toBe('маркетолог');
    expect(whoLabel(reminder({ role: null }))).toBe('сессия');
  });

  it('значок: одно — «⏰ время», несколько — число; завтра 10:00 — локальные 10:00', () => {
    expect(badgeLabel([], NOW)).toBe('');
    expect(badgeLabel([reminder({ status: 'pending', due_ts: NOW + 3_600_000 })], NOW)).toBe('⏰ 16:00');
    expect(badgeLabel([reminder({ status: 'pending' }), reminder({ id: 2, status: 'pending' })], NOW)).toBe('⏰ 2');
    const t = new Date(tomorrowTen(NOW));
    expect([t.getDate(), t.getHours(), t.getMinutes()]).toEqual([4, 10, 0]);
    expect(fromLocalInput(toLocalInput(NOW))).toBe(NOW);
    expect(fromLocalInput('')).toBeNull();
  });
});

describe('Ф34-2: стор — /sync, SSE и главная', () => {
  it('/sync.reminders попадает в стор; старшая по seq побеждает; закрытое перестаёт быть сработавшим', async () => {
    let calls = 0;
    const { fetchMock } = fakeFetch((s) => {
      if (s.path !== '/api/v1/sync') return undefined;
      calls += 1;
      if (calls === 1) return jsonResponse({ messages: [], channels: [], max_seq: 2, more: false, reminders: [reminder({ seq: 11 })] });
      if (calls === 2) {
        return jsonResponse({ messages: [], channels: [], max_seq: 3, more: false, reminders: [reminder({ seq: 9, status: 'done' })] });
      }
      return jsonResponse({ messages: [], channels: [], max_seq: 4, more: false, reminders: [reminder({ seq: 12, status: 'done' })] });
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.runSync();
    expect(s.state.reminders.items[1]!.status).toBe('fired');
    await s.runSync();
    expect(s.state.reminders.items[1]!.status).toBe('fired'); // seq 9 старше 11
    await s.runSync();
    expect(firedReminders(Object.values(s.state.reminders.items))).toEqual([]);
  });

  it('applyReminder: опоздавший REST не откатывает свежее', () => {
    const st = createReminderState();
    applyReminder(st, reminder({ seq: 5, status: 'pending' }));
    applyReminder(st, reminder({ seq: 4, status: 'cancelled' }));
    expect(st.items[1]!.status).toBe('pending');
  });

  it('кадр SSE reminder запускает /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      readyState: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      close: () => {},
    };
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('reminder')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
  });

  it('loadHome: строки-напоминания (last: null) уходят в стор напоминаний, строки бесед остаются', async () => {
    const row = {
      channel: channel(),
      project: { id: 3, name: 'Umbrella', color: null },
      last: null,
      card: null,
      draft_count: 0,
      reminder: reminder(),
    };
    const { fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/home/react' ? jsonResponse([reactItem(), row]) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.loadHome();
    expect(s.state.home.react.map((i) => i.channel.id)).toEqual([9]);
    expect(s.state.reminders.items[1]!.status).toBe('fired');
  });
});

describe('Ф34-2: главная и «Ждёт»', () => {
  const projects = [project(), project({ id: 4, name: 'Личное' })];
  const home = (reminders: Reminder[], react: HomeReactItem[] = []) => ({
    react,
    drafts: [],
    runs: [],
    sent: [],
    reminders,
  });

  it('доска: сработавшее — в столбце project_id напоминания; null — в «Неразобранном»; ждущее — нигде', () => {
    const board = buildBoard(
      projects,
      home([
        reminder({ id: 1, project_id: 4 }),
        reminder({ id: 2, project_id: null }),
        reminder({ id: 3, status: 'pending' }),
      ]),
      NOW,
    );
    expect(board.columns.find((c) => c.project.id === 4)!.reminders.map((r) => r.id)).toEqual([1]);
    expect(board.columns.find((c) => c.project.id === 3)!.reminders).toEqual([]);
    expect(board.unsorted?.reminders.map((r) => r.id)).toEqual([2]);
    expect(board.columns.find((c) => c.project.id === 4)!.waitCount).toBe(1);
  });

  it('«Ждёт»: напоминание — элемент reminder и входит в счётчик решений', () => {
    const input = {
      projects,
      home: { react: [], drafts: [] },
      work: { tasks: {}, pending: {}, stale: {} },
      reminders: [reminder()],
    };
    expect(buildWaiting(input).map((i) => i.kind)).toEqual(['reminder']);
    expect(homeDecisionCount(input)).toBe(1);
  });

  it('столбец рисует строку беседы и строку напоминания рядом', () => {
    const board = buildBoard(projects, home([reminder()], [reactItem()]), NOW);
    const wrapper = mount(ProjectColumn, { props: { column: board.columns.find((c) => c.project.id === 3)! } });
    expect(wrapper.find('.home-react-row').text()).toContain('Обычная беседа');
    const card = wrapper.get('[data-section="reminders"] .reminder-card');
    expect(card.text()).toContain('⏰');
    expect(card.text()).toContain('Иван Петров');
    expect(card.text()).toContain('уточнить сроки');
    expect(card.text()).toContain('маркетолог');
    expect(wrapper.get('.board-section-title').text()).toContain('2');
  });

  it('WaitList рисует карточку напоминания', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    store.state.reminders.items[1] = reminder();
    const wrapper = mount(WaitList);
    await flushPromises();
    expect(wrapper.find('.wait-item--reminder .reminder-card').exists()).toBe(true);
    wrapper.unmount();
  });

  it('заметка и название — только текстом, не HTML', () => {
    const wrapper = mount(ReminderCard, {
      props: { reminder: reminder({ note: XSS, channel: { id: 7, title: XSS, project_id: 3, archived: false } }) },
    });
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.find('script').exists()).toBe(false);
    expect(wrapper.text()).toContain(XSS);
  });
});

describe('Ф34-2: кнопки строки напоминания', () => {
  it('«Готово» шлёт close:true; «Отложить» — на час, до завтра 10:00 и своё время с верным at', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path.startsWith('/api/v1/reminders/') && s.method === 'PATCH' ? jsonResponse(reminder({ seq: 20, status: 'pending' })) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(ReminderCard, { props: { reminder: reminder() }, attachTo: document.body });
    await wrapper.get('button.reminder-done').trigger('click');
    await flushPromises();
    await wrapper.get('button.reminder-snooze').trigger('click');
    await wrapper.get('button.reminder-snooze-hour').trigger('click');
    await flushPromises();
    await wrapper.get('button.reminder-snooze').trigger('click');
    await wrapper.get('button.reminder-snooze-tomorrow').trigger('click');
    await flushPromises();
    await wrapper.get('button.reminder-snooze').trigger('click');
    await wrapper.get('input.reminder-custom-input').setValue('2026-10-05T09:30');
    await wrapper.get('button.reminder-snooze-custom').trigger('click');
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH');
    expect(patches.map((p) => p.path)).toEqual(['/api/v1/reminders/1', '/api/v1/reminders/1', '/api/v1/reminders/1', '/api/v1/reminders/1']);
    expect(patches[0]!.body).toEqual({ close: true });
    expect(patches[1]!.body).toEqual({ at: new Date(NOW + 3_600_000).toISOString() });
    expect(patches[2]!.body).toEqual({ at: new Date(2026, 9, 4, 10, 0, 0).toISOString() });
    expect(patches[3]!.body).toEqual({ at: new Date(2026, 9, 5, 9, 30, 0).toISOString() });
    wrapper.unmount();
  });

  it('«Открыть беседу» открывает беседу напоминания', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const spy = vi.spyOn(store, 'openChannelAt').mockResolvedValue();
    const wrapper = mount(ReminderCard, { props: { reminder: reminder() } });
    await wrapper.get('button.reminder-open').trigger('click');
    expect(spy).toHaveBeenCalledWith(3, 7);
  });

  it('409 на правку — строка про изменение и перечитывание', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.method === 'PATCH' ? jsonResponse({ detail: 'закрыто' }, 409) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(ReminderCard, { props: { reminder: reminder() } });
    await wrapper.get('button.reminder-done').trigger('click');
    await flushPromises();
    expect(wrapper.get('.reminder-error').text()).toContain('уже изменилось');
    expect(sent.some((s) => s.path === '/api/v1/reminders' && s.query.includes('channel_id=7'))).toBe(true);
  });
});

describe('Ф34-2: шапка беседы', () => {
  it('значок у беседы с pending: «⏰ время», при нескольких — число; клик раскрывает список', async () => {
    // Часы теста — 15:00: срок «через час» с 23:00 до полуночи был бы «завтра 00:39», а не «⏰ 16:00».
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    store.state.reminders.items[1] = reminder({ status: 'pending', due_ts: Date.now() + 3_600_000 });
    const wrapper = mount(ChannelView, { props: { channelId: 7 }, attachTo: document.body });
    await flushPromises();
    const badge = wrapper.get('button.reminder-badge');
    expect(badge.text()).toMatch(/^⏰ \d/);
    expect(wrapper.find('.channel-reminders').exists()).toBe(false);
    await badge.trigger('click');
    const panel = wrapper.get('.channel-reminders');
    expect(panel.text()).toContain('уточнить сроки');
    expect(panel.find('button.reminder-unset').exists()).toBe(true);
    expect(panel.find('button.reminder-move').exists()).toBe(true);
    store.state.reminders.items[2] = reminder({ id: 2, status: 'pending', due_ts: Date.now() + 7_200_000 });
    await flushPromises();
    expect(wrapper.get('button.reminder-badge').text()).toBe('⏰ 2');
    wrapper.unmount();
  });

  it('без pending значка нет; «Напомнить…» есть, в архиве — нет', async () => {
    const { fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(ChannelView, { props: { channelId: 7 }, attachTo: document.body });
    await flushPromises();
    expect(wrapper.find('button.reminder-badge').exists()).toBe(false);
    expect(wrapper.find('button.remind-button').exists()).toBe(true);
    wrapper.unmount();
    store.state.sync.channelsById = { 7: channel({ archived: true }) };
    const archived = mount(ChannelView, { props: { channelId: 7 }, attachTo: document.body });
    await flushPromises();
    expect(archived.find('button.remind-button').exists()).toBe(false);
    archived.unmount();
  });

  it('«Снять» и «Перенести» шлют close / at', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.method === 'PATCH' ? jsonResponse(reminder({ seq: 30, status: 'pending' })) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    store.state.reminders.items[1] = reminder({ status: 'pending' });
    const wrapper = mount(ChannelReminders, { props: { channelId: 7, archived: false } });
    await wrapper.get('button.reminder-move').trigger('click');
    await wrapper.get('input.reminder-move-input').setValue('2026-10-06T08:15');
    await wrapper.get('button.reminder-move-ok').trigger('click');
    await flushPromises();
    await wrapper.get('button.reminder-unset').trigger('click');
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH').map((s) => s.body);
    expect(patches).toEqual([{ at: new Date(2026, 9, 6, 8, 15, 0).toISOString() }, { close: true }]);
  });

  it('«Напомнить…»: шлёт время, заметку и cancel_if_reply (по умолчанию включён)', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.method === 'POST' && s.path === '/api/v1/reminders' ? jsonResponse(reminder({ status: 'pending', seq: 40 }), 201) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(ChannelReminders, { props: { channelId: 7, archived: false } });
    expect((wrapper.get('.reminder-cancel-if-reply input').element as HTMLInputElement).checked).toBe(true);
    await wrapper.get('input.reminder-at').setValue('2026-10-04T12:00');
    await wrapper.get('input.reminder-note-input').setValue('позвонить');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    const post = sent.find((s) => s.method === 'POST' && s.path === '/api/v1/reminders')!;
    expect(post.body).toEqual({
      channel_id: 7,
      at: new Date(2026, 9, 4, 12, 0, 0).toISOString(),
      note: 'позвонить',
      cancel_if_reply: true,
    });
    expect(store.state.reminders.items[1]!.status).toBe('pending');
    await wrapper.get('.reminder-cancel-if-reply input').setValue(false);
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    const second = sent.filter((s) => s.method === 'POST' && s.path === '/api/v1/reminders')[1]!;
    expect((second.body as { cancel_if_reply: boolean }).cancel_if_reply).toBe(false);
  });

  it('ошибки 422 и 429 показываются текстом detail', async () => {
    let n = 0;
    const { fetchMock } = fakeFetch((s) => {
      if (s.method !== 'POST') return undefined;
      n += 1;
      return n === 1
        ? jsonResponse({ detail: 'at: слишком рано, не раньше чем через минуту' }, 422)
        : jsonResponse({ detail: 'на беседе уже 5 ждущих напоминаний' }, 429);
    });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(ChannelReminders, { props: { channelId: 7, archived: false } });
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(wrapper.get('.reminder-error').text()).toBe('at: слишком рано, не раньше чем через минуту');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(wrapper.get('.reminder-error').text()).toBe('на беседе уже 5 ждущих напоминаний');
  });
});
