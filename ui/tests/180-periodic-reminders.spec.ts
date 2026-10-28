// Ф89-1 (`docs/architecture.md`): повторяемые напоминания и напоминания без беседы — карточка
// (период, «пропущено», «последний раз», «Сделано»/«Снять»), форма беседы с периодом, панель «⋯ → Напоминания».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Reminder } from '../src/api/types';
import ChannelReminders from '../src/components/ChannelReminders.vue';
import ReminderCard from '../src/components/ReminderCard.vue';
import RemindersPanel from '../src/components/RemindersPanel.vue';
import TopMenu from '../src/components/TopMenu.vue';
import { store } from '../src/store/instance';
import { createReminderState } from '../src/store/reminders';
import { everyOf } from '../src/util/reminders';

const NOW = new Date(2026, 9, 23, 12, 0, 0).getTime();

function reminder(overrides: Partial<Reminder> = {}): Reminder {
  return {
    id: 5,
    channel_id: 7,
    project_id: 3,
    due_ts: NOW + 86_400_000,
    note: 'проверить отчёт',
    cancel_if_reply: false,
    status: 'fired',
    by: 'session',
    role: 'маркетолог',
    created_ts: NOW - 1e9,
    updated_ts: NOW,
    fired_ts: NOW - 60_000,
    delivered_ts: null,
    closed_ts: null,
    end_reason: null,
    waiting: true,
    seq: 10,
    channel: { id: 7, title: 'Иван Петров', project_id: 3, archived: false },
    every: 'day',
    every_text: 'каждый день в 09:00',
    at_hm: '09:00',
    tz: 'Europe/Moscow',
    missed: 2,
    done_count: 4,
    last_done_ts: NOW - 86_400_000,
    last_done_role: null,
    ...overrides,
  };
}

interface Sent {
  method: string;
  path: string;
  query: string;
  body: unknown;
}

function stub(handler: (s: Sent) => Response | undefined = () => undefined): Sent[] {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: url.pathname,
      query: url.search,
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
    };
    sent.push(s);
    const custom = handler(s);
    if (custom !== undefined) return custom;
    const json = (b: unknown) => new Response(JSON.stringify(b), { status: 200, headers: { 'content-type': 'application/json' } });
    if (s.path === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
    if (s.path === '/api/v1/reminders' && s.method === 'GET') return json({ items: [] });
    if (s.method === 'GET') return json([]);
    return json(reminder({ status: 'pending' }));
  });
  vi.stubGlobal('fetch', fetchMock);
  return sent;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.reminders = createReminderState();
  store.state.projects = [{ id: 3, name: 'Umbrella', color: '#f00', sort: 0, muted: false, workdir: '/p', unread: 0 }];
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф89-1: карточка повторяемого', () => {
  it('период, «пропущено K», «последний раз», «сработало в»; кнопки «Сделано» и «Снять» с подтверждением', async () => {
    const sent = stub();
    const w = mount(ReminderCard, { props: { reminder: reminder() } });
    expect(w.text()).toContain('каждый день в 09:00');
    expect(w.get('.reminder-missed').text()).toBe('пропущено 2');
    expect(w.get('.reminder-last-done').text()).toContain('последний раз:');
    expect(w.get('.reminder-last-done').text()).toContain('Владелец');
    expect(w.text()).toContain('сработало в');
    expect(w.text()).not.toContain('срок был');

    await w.get('button.reminder-done').trigger('click');
    await flushPromises();
    await w.get('button.reminder-stop').trigger('click');
    expect(sent.filter((s) => s.method === 'PATCH')).toHaveLength(1);
    await w.get('button.reminder-stop-confirm').trigger('click');
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH');
    expect(patches[0]!.body).toEqual({ close: true });
    expect(patches[1]!.body).toEqual({ stop: true });
    w.unmount();
  });

  it('K = 0 — без значка; разовое — без периода и «Снять»', () => {
    stub();
    const w = mount(ReminderCard, { props: { reminder: reminder({ missed: 0 }) } });
    expect(w.find('.reminder-missed').exists()).toBe(false);
    const once = mount(ReminderCard, {
      props: { reminder: reminder({ every: null, every_text: null, missed: 0, last_done_ts: null }) },
    });
    expect(once.find('.reminder-every').exists()).toBe(false);
    expect(once.find('button.reminder-stop').exists()).toBe(false);
    expect(once.text()).toContain('срок был');
    expect(once.text()).toContain('Готово');
  });

  it('без беседы — нет «Открыть», заголовок «Напоминание <роль>»', () => {
    stub();
    const w = mount(ReminderCard, { props: { reminder: reminder({ channel_id: null, channel: null }) } });
    expect(w.find('button.reminder-open').exists()).toBe(false);
    expect(w.get('.reminder-title').text()).toBe('Напоминание маркетолог');
  });
});

describe('Ф89-1: форма беседы', () => {
  it('период «по будням» уходит every без cancel_if_reply; разово — как прежде', async () => {
    const sent = stub();
    const w = mount(ChannelReminders, { props: { channelId: 7, archived: false } });
    await w.get('select.reminder-every-select').setValue('weekdays');
    expect(w.find('.reminder-cancel-if-reply').exists()).toBe(false);
    await w.get('form').trigger('submit');
    await flushPromises();
    const first = sent.find((s) => s.method === 'POST')!;
    expect(first.body).toMatchObject({ channel_id: 7, every: 'weekdays' });
    expect(first.body).not.toHaveProperty('cancel_if_reply');

    await w.get('select.reminder-every-select').setValue('');
    await w.get('form').trigger('submit');
    await flushPromises();
    const second = sent.filter((s) => s.method === 'POST')[1]!;
    expect(second.body).toMatchObject({ channel_id: 7, cancel_if_reply: true });
    expect(second.body).not.toHaveProperty('every');
    w.unmount();
  });

  it('«каждые N ч» → "4h"; у повторяемого в списке «Сделано» и «Снять»', async () => {
    expect(everyOf('hours', '4')).toBe('4h');
    expect(everyOf('hours', 'x')).toBeUndefined();
    expect(everyOf('', '4')).toBeNull();
    const sent = stub();
    store.state.reminders.items = { 5: reminder({ status: 'fired' }) };
    const w = mount(ChannelReminders, { props: { channelId: 7, archived: false } });
    expect(w.text()).toContain('каждый день в 09:00');
    await w.get('button.reminder-done').trigger('click');
    await flushPromises();
    await w.get('button.reminder-unset').trigger('click');
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH');
    expect(patches[0]!.body).toEqual({ close: true });
    expect(patches[1]!.body).toEqual({ stop: true });
    w.unmount();
  });
});

describe('Ф89-1: панель «⋯ → Напоминания»', () => {
  const rows = [
    reminder({ id: 1, new_since_done: 3 }),
    reminder({ id: 2, channel_id: null, channel: null, every: null, every_text: null, missed: 0, status: 'pending', new_since_done: null, note: 'без беседы заметка' }),
    reminder({ id: 3, project_id: 4, status: 'pending', missed: 0 }),
  ];

  it('группы по проектам, поля строки, кнопки с телом запроса', async () => {
    const sent = stub((s) =>
      s.path === '/api/v1/reminders' && s.method === 'GET'
        ? new Response(JSON.stringify({ items: rows }), { status: 200, headers: { 'content-type': 'application/json' } })
        : undefined,
    );
    const w = mount(RemindersPanel);
    await flushPromises();
    expect(sent.find((s) => s.method === 'GET' && s.path === '/api/v1/reminders')!.query).toContain('counts=1');
    expect(w.findAll('[data-test="reminders-group"]')).toHaveLength(2);
    expect(w.findAll('.reminders-group-title').map((t) => t.text())).toEqual(['Umbrella', 'проект #4']);
    const first = w.get('[data-reminder-id="1"]');
    expect(first.text()).toContain('Иван Петров');
    expect(first.text()).toContain('каждый день в 09:00');
    expect(first.text()).toContain('пропущено 2');
    expect(first.text()).toContain('последний раз:');
    expect(first.text()).toContain('новых с прошлого раза: 3');
    const bare = w.get('[data-reminder-id="2"]');
    expect(bare.text()).toContain('без беседы');
    expect(bare.text()).toContain('разово');
    expect(bare.find('.reminders-channel').exists()).toBe(false);

    await first.get('button.reminder-stop').trigger('click');
    await first.get('button.reminder-stop-confirm').trigger('click');
    await flushPromises();
    await w.get('[data-reminder-id="2"] button.reminder-done').trigger('click');
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH');
    expect(patches.map((p) => [p.path, p.body])).toEqual([
      ['/api/v1/reminders/1', { stop: true }],
      ['/api/v1/reminders/2', { close: true }],
    ]);
    w.unmount();
  });

  it('клик по беседе открывает её окном поверх', async () => {
    stub((s) =>
      s.path === '/api/v1/reminders' && s.method === 'GET'
        ? new Response(JSON.stringify({ items: [rows[0]!] }), { status: 200, headers: { 'content-type': 'application/json' } })
        : undefined,
    );
    const spy = vi.spyOn(store, 'openChannelOverlay').mockResolvedValue();
    const w = mount(RemindersPanel);
    await flushPromises();
    await w.get('.reminders-channel').trigger('click');
    expect(spy).toHaveBeenCalledWith(7);
    w.unmount();
  });

  it('пусто — «Открытых напоминаний нет»; пункт меню «⋯» открывает панель', async () => {
    stub();
    const w = mount(RemindersPanel);
    await flushPromises();
    expect(w.text()).toContain('Открытых напоминаний нет');
    w.unmount();

    const menu = mount(TopMenu, { attachTo: document.body });
    await menu.get('.top-menu-button').trigger('click');
    await menu.get('[data-test="reminders-open"]').trigger('click');
    await flushPromises();
    expect(menu.find('.reminders-panel').exists()).toBe(true);
    menu.unmount();
  });
});
