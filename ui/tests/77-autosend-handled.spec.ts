// Ф37-1 A4/C1/C2 (`docs/architecture.md`): «Без подтверждения ▾» в шапке беседы (включение с
// подтверждением, выключение без, отказ ядра виден, чип, скрыт у беседы без проекта), подпись «агент: <роль>» у
// сообщения и в «Отправлено сегодня».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, Message, OutboxListItem, Project } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import MessageRow from '../src/components/MessageRow.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import { store } from '../src/store/instance';
import { buildBoard } from '../src/util/board';
import { startOfDay } from '../src/util/home';

const NOW = new Date(2026, 9, 4, 12, 0, 0).getTime();

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: 5,
    ext_id: '5',
    kind: 'group',
    title: 'Продавцы',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 3,
    ...overrides,
  };
}

function project(): Project {
  return { id: 3, name: 'Umbrella', color: null, sort: 0, muted: false, workdir: null, unread: 0 };
}

function msg(overrides: Partial<Message> = {}): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 3,
    ext_id: '1',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: true,
    kind: 'text',
    text: 'привет',
    entities_json: null,
    attachments_json: null,
    forward_json: null,
    mentions_me: false,
    refs_json: null,
    reactions_json: null,
    ts: 1000,
    edited_ts: null,
    deleted: false,
    outbox_id: null,
    outbox_status: null,
    sender: { id: 1, name: 'я', handle: null },
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let calls: { method: string; path: string; body: unknown }[] = [];
let putStatus = 200;

beforeEach(() => {
  window.localStorage.clear();
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  calls = [];
  putStatus = 200;
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  store.state.projects = [project()];
  store.state.currentProjectId = 3;
  store.state.currentChannelId = null;
  store.state.peerMsgs.agentSubs = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const body = init?.body ? JSON.parse(String(init.body)) : null;
      calls.push({ method, path: url.pathname, body });
      if (url.pathname.endsWith('/autosend')) {
        if (putStatus !== 200) return json({ detail: 'беседа в «Неразобранном»' }, putStatus);
        return json({ autosend: body.on ? [body.role] : [], changed: true });
      }
      if (url.pathname.endsWith('/subs')) {
        return json({ items: [{ id: 1, project_id: 3, role: 'маркетолог', live: true, mode: 'direct', engine: 'claude' }], channels: [] });
      }
      if (url.pathname.endsWith('/messages')) return json({ messages: [], next_before: null });
      return json([]);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф37-1 A4: «Без подтверждения ▾» в шапке', () => {
  // `refreshAgentSubs` в сторе ограничен по частоте (общий таймер на процесс) — живые подписки задаём в стор прямо.
  const SUB = { id: 1, project_id: 3, role: 'маркетолог', role_no: 1, mode: 'direct', engine: 'claude', session_name: null, live: true, listening: true, waiting: false } as const;
  async function open(ch: Channel, subs: readonly (typeof SUB)[] = [SUB]): Promise<ReturnType<typeof mount>> {
    store.state.peerMsgs.agentSubs = [...subs] as never;
    store.state.sync.channelsById[5] = ch;
    const wrapper = mount(ChannelView, { props: { channelId: 5 } });
    await flushPromises();
    return wrapper;
  }

  it('меню: роль живой подписки; включение — с подтверждением фразой, выключение — сразу', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const wrapper = await open(channel());
    await wrapper.get('.autosend-button').trigger('click');
    await flushPromises();
    const boxes = wrapper.findAll('.autosend-item input');
    expect(boxes).toHaveLength(1);
    expect(wrapper.get('.autosend-item').text()).toBe('маркетолог');
    await boxes[0]!.setValue(true);
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0]![0]).toBe(
      'Любая ваша сессия в проекте, подписанная ролью «маркетолог», будет отправлять в эту беседу от вашего имени сразу, без подтверждения, и нажимать кнопки ботов. Продолжить?',
    );
    const put = calls.find((c) => c.method === 'PUT');
    expect(put?.path).toContain('/channels/5/autosend');
    expect(put?.body).toEqual({ role: 'маркетолог', on: true });
    wrapper.unmount();

    // выключение: беседа уже разрешена — confirm не зовётся
    confirm.mockClear();
    calls.length = 0;
    const wrapper2 = await open(channel({ autosend: ['маркетолог'] }));
    await wrapper2.get('.autosend-button').trigger('click');
    await flushPromises();
    await wrapper2.get('.autosend-item input').setValue(false);
    await flushPromises();
    expect(confirm).not.toHaveBeenCalled();
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ role: 'маркетолог', on: false });
    wrapper2.unmount();
  });

  it('отказ в подтверждении — запроса нет', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    const wrapper = await open(channel());
    await wrapper.get('.autosend-button').trigger('click');
    await flushPromises();
    await wrapper.get('.autosend-item input').setValue(true);
    await flushPromises();
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);
    wrapper.unmount();
  });

  it('отказ ядра (422) виден текстом рядом с контролом', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    putStatus = 422;
    const wrapper = await open(channel());
    await wrapper.get('.autosend-button').trigger('click');
    await flushPromises();
    await wrapper.get('.autosend-item input').setValue(true);
    await flushPromises();
    expect(wrapper.get('.autosend-control [role=alert]').text()).toContain('Неразобранном');
    wrapper.unmount();
  });

  it('чип «без подтверждения: роли» — когда список не пуст; роль без живой подписки остаётся в меню', async () => {
    const wrapper = await open(channel({ autosend: ['маркетолог', 'аналитик'] }));
    expect(wrapper.get('.autosend-chip').text()).toBe('без подтверждения: маркетолог, аналитик');
    await wrapper.get('.autosend-button').trigger('click');
    await flushPromises();
    expect(wrapper.findAll('.autosend-item').map((l) => l.text())).toEqual(['маркетолог', 'аналитик']);
    wrapper.unmount();
    const empty = await open(channel({ autosend: [] }));
    expect(empty.find('.autosend-chip').exists()).toBe(false);
    empty.unmount();
  });

  it('нет ролей ни живых, ни разрешённых — «нет сессий с ролью в проекте»', async () => {
    const wrapper = await open(channel(), []);
    await wrapper.get('.autosend-button').trigger('click');
    await flushPromises();
    expect(wrapper.get('.autosend-empty').text()).toBe('нет сессий с ролью в проекте');
    wrapper.unmount();
  });

  it('беседа без проекта, в архиве, заготовка, no_reply — контрола нет', async () => {
    for (const ch of [
      channel({ project_id: null }),
      channel({ archived: true }),
      channel({ new_to: { kind: 'tg_username', username: 'x' } }),
      channel({ can_reply: false }),
    ]) {
      const wrapper = await open(ch);
      expect(wrapper.find('.autosend-button').exists()).toBe(false);
      wrapper.unmount();
    }
  });

  it('роль — только текстом, без разметки', async () => {
    const wrapper = await open(channel({ autosend: ['<b>x</b>'] }));
    expect(wrapper.get('.autosend-chip').element.querySelector('b')).toBeNull();
    wrapper.unmount();
  });
});

describe('Ф37-1 C1/C2: подпись «агент: …»', () => {
  it('у своего сообщения с outbox_auto_role — «агент: роль»; без поля — нет', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ outbox_auto_role: 'маркетолог' }) } });
    expect(wrapper.get('.auto-role-mark').text()).toBe('агент: маркетолог');
    expect(mount(MessageRow, { props: { message: msg({ outbox_auto_role: null }) } }).find('.auto-role-mark').exists()).toBe(false);
    expect(mount(MessageRow, { props: { message: msg() } }).find('.auto-role-mark').exists()).toBe(false);
  });

  it('«Отправлено сегодня»: у строки с auto_role — «без подтверждения · роль»', async () => {
    const sentItem = (id: number, auto_role: string | null): OutboxListItem => ({
      id,
      channel_id: 5,
      project_id: 3,
      channel_title: 'Продавцы',
      text: `текст ${id}`,
      status: 'sent',
      origin: 'api',
      error: null,
      attempts: 1,
      created_ts: NOW - 5000,
      sent_ts: NOW - 4000,
      ext_id: 'x',
      auto_role,
    });
    const board = buildBoard(
      [project()],
      { react: [], drafts: [], runs: [], sent: [sentItem(1, 'маркетолог'), sentItem(2, null)] },
      NOW,
    );
    const wrapper = mount(ProjectColumn, { props: { column: board.columns[0]! } });
    await wrapper.get('.board-sent .board-section-toggle').trigger('click');
    const marks = wrapper.findAll('.home-sent-auto');
    expect(marks).toHaveLength(1);
    expect(marks[0]!.text()).toBe('без подтверждения · маркетолог');
  });
});
