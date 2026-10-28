// Ф12-5 C (`docs/architecture.md`): кнопка «Новое сообщение» только во вкладке
// настоящего проекта; форма — поля по виду аккаунта, умолчание «Откуда» из `uiPrefs`, тема обязательна у почты,
// отказ ядра — текстом под открытой формой; успех — беседа из ответа в сторе и открыта без `/sync`, фокус в поле
// ввода. Беседа-заготовка: строка «кому», причина из ядра вместо поля ввода, скрепка (Ф14-2 D4 — у всех).
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Account, Channel, Project, StatusResponse } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import Composer from '../src/components/Composer.vue';
import { store } from '../src/store/instance';

function account(overrides: Partial<Account>): Account {
  return {
    id: 1,
    source: 'telegram',
    title: 'Личный',
    status: 'ok',
    last_ok_ts: null,
    last_error: null,
    flood_until: null,
    new_message: true,
    address: null,
    ...overrides,
  } as Account;
}

function project(id: number, name: string): Project {
  return { id, name, color: null, sort: id, muted: false, workdir: null, unread: 0 };
}

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 50,
    ext_id: 'new:0123456789abcdef0123456789abcdef',
    kind: 'mail_thread',
    title: 'client@example.com',
    tier: null,
    muted: false,
    archived: false,
    map_reason: 'новая беседа',
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 1000,
    unread: 0,
    project_id: 3,
    source: 'mail',
    can_reply: true,
    new_to: { kind: 'email', to: 'client@example.com', subject: 'Подпись для ООО', from: null },
    ...overrides,
  };
}

const ACCOUNTS = [
  account({ id: 1, source: 'telegram', title: 'Личный' }),
  account({ id: 2, source: 'slack', title: 'Globex', new_message: false }),
  account({ id: 5, source: 'mail', title: 'Почта ИИП', address: 'me@umbrella.example' }),
];

type Handler = (url: URL, init: RequestInit | undefined) => Response | undefined;
let handler: Handler = () => undefined;
const calls: string[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  window.localStorage.clear();
  calls.length = 0;
  handler = () => undefined;
  store.state.sync.channelsById = {};
  store.state.sync.threads = {};
  store.state.openThread = null;
  store.state.homeOpen = false;
  store.state.currentProjectId = 3;
  store.state.currentChannelId = null;
  store.state.composerFocusChannelId = null;
  store.state.projects = [project(1, 'Globex'), project(3, 'Umbrella')];
  store.state.status = { accounts: ACCOUNTS } as unknown as StatusResponse;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      calls.push(`${init?.method ?? 'GET'} ${url.pathname}`);
      const custom = handler(url, init);
      if (custom !== undefined) return custom;
      if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
      return jsonResponse([]);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

// Ф14-2: кнопка «Новое сообщение» ушла из списка бесед в верхнюю полосу, форма переделана («Кому» с поиском,
// «Откуда» по адресату, «По правилам») — тесты C1/C2 заменены тестами 14–15 в `48-new-anywhere.spec.ts`.

describe('Ф12-5 C3: беседа-заготовка в ленте', () => {
  it('can_reply=true — строка «кому» и поле ввода со скрепкой (почта)', async () => {
    store.state.sync.channelsById[50] = channel({});
    const wrapper = mount(ChannelView, { props: { channelId: 50 } });
    await flushPromises();
    expect(wrapper.get('.new-recipient-line').text()).toBe(
      'Новая беседа · кому: client@example.com · Подпись для ООО',
    );
    expect(wrapper.findComponent(Composer).exists()).toBe(true);
    expect(wrapper.find('[aria-label="Прикрепить"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('Telegram-заготовка — строка «кому: @username», скрепка есть (Ф14-2 D4)', async () => {
    store.state.sync.channelsById[51] = channel({
      id: 51,
      kind: 'dm',
      source: 'telegram',
      title: '@petr',
      new_to: { kind: 'tg_username', username: 'petr' },
    });
    const wrapper = mount(ChannelView, { props: { channelId: 51 } });
    await flushPromises();
    expect(wrapper.get('.new-recipient-line').text()).toBe('Новая беседа · кому: @petr');
    expect(wrapper.findComponent(Composer).exists()).toBe(true);
    expect(wrapper.find('[aria-label="Прикрепить"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('can_reply=false — вместо поля ввода причина из ядра', async () => {
    store.state.sync.channelsById[50] = channel({
      can_reply: false,
      can_reply_reason: 'сообщение ушло — беседа привяжется, когда придёт его копия',
    });
    const wrapper = mount(ChannelView, { props: { channelId: 50 } });
    await flushPromises();
    expect(wrapper.findComponent(Composer).exists()).toBe(false);
    expect(wrapper.get('.no-reply').text()).toBe('сообщение ушло — беседа привяжется, когда придёт его копия');
    wrapper.unmount();
  });
});
