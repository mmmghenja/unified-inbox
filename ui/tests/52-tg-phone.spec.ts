// Ф34-3 A5/П4/B6 (`docs/architecture.md`): Telegram по номеру телефона — «Кому» принимает номер
// (`to` строкой), подсказка лимита из `/status`, 0 — поиск выключен, 422 показан как есть; полный номер видит
// владелец в шапке заготовки, на карточке черновика и в окне черновика; в списке бесед — маска из `title`.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Account, Channel, OutboxListItem, StatusResponse } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import DraftRow from '../src/components/DraftRow.vue';
import NewMessageForm from '../src/components/NewMessageForm.vue';
import { store } from '../src/store/instance';
import { draftTargetLabel, newRecipientLabel, newRecipientWho } from '../src/util/home';
import { looksLikePhone, parseNewAddress } from '../src/util/newMessage';

const PHONE = '+79000000001';
const MASK = '+7 ••• ••• 00 01';

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
    tg_phone_daily: 5,
    ...overrides,
  } as Account;
}

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: 60,
    ext_id: 'new:0123456789abcdef0123456789abcdef',
    kind: 'dm',
    title: MASK,
    tier: null,
    muted: false,
    archived: false,
    map_reason: 'новая беседа',
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 1000,
    unread: 0,
    project_id: null,
    source: 'telegram',
    can_reply: false,
    new_to: { kind: 'tg_phone', phone: PHONE },
    ...overrides,
  };
}

function draft(overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id: 7,
    channel_id: 60,
    project_id: 1,
    channel_title: MASK,
    text: 'Здравствуйте!',
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Umbrella',
    source: 'telegram',
    kind: 'dm',
    author: 'mcp:iip',
    reply_to: null,
    thread: null,
    new_to: { kind: 'tg_phone', phone: PHONE },
    ...overrides,
  } as OutboxListItem;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let posted: unknown[] = [];
let newResponse: () => Response = () => jsonResponse({ channel: channel(), created: true });

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  posted = [];
  newResponse = () => jsonResponse({ channel: channel(), created: true });
  store.state.sync.channelsById = {};
  store.state.currentProjectId = 3;
  store.state.currentChannelId = null;
  store.state.projects = [];
  store.state.status = { accounts: [account({})] } as unknown as StatusResponse;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname.endsWith('/channels/new')) {
        posted.push(JSON.parse(String(init?.body)));
        return newResponse();
      }
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

async function typeTo(w: VueWrapper, value: string): Promise<void> {
  await w.get('.new-message-to').setValue(value);
  await new Promise((resolve) => setTimeout(resolve, 260));
  await flushPromises();
}

describe('Ф34-3 П4: полный номер для подтверждения', () => {
  it('newRecipientWho / Label / draftTargetLabel для tg_phone — полный номер', () => {
    expect(newRecipientWho({ kind: 'tg_phone', phone: PHONE })).toBe(PHONE);
    expect(newRecipientLabel({ kind: 'tg_phone', phone: PHONE })).toBe(`${PHONE} (новая беседа)`);
    expect(draftTargetLabel(draft())).toBe(`кому: ${PHONE} (новая беседа)`);
    expect(newRecipientWho({ kind: 'tg_phone', phone: '' })).toBeNull();
  });

  it('карточка черновика показывает полный номер, заголовок — маску', () => {
    const row = mount(DraftRow, { props: { item: draft() } });
    expect(row.text()).toContain(PHONE);
    row.unmount();
  });

  it('шапка заготовки показывает полный номер, а title беседы — маска', async () => {
    store.state.sync.channelsById[60] = channel();
    const w = mount(ChannelView, { props: { channelId: 60 } });
    await flushPromises();
    expect(w.text()).toContain(PHONE);
    expect(w.text()).toContain(MASK);
    w.unmount();
  });

  it('после привязки (new_to нет) номер из шапки пропал', async () => {
    store.state.sync.channelsById[60] = channel({ new_to: null, title: 'Пётр', can_reply: true });
    const w = mount(ChannelView, { props: { channelId: 60 } });
    await flushPromises();
    expect(w.text()).not.toContain(PHONE);
    w.unmount();
  });
});

describe('Ф34-3 A5: форма «Новое сообщение» с номером', () => {
  it('looksLikePhone / parseNewAddress: номер, но не имя, адрес и короткая цифра', () => {
    expect(looksLikePhone('+7 (900) 000-00-01')).toBe(true);
    expect(looksLikePhone('89000000001')).toBe(true);
    expect(looksLikePhone('12345')).toBe(false);
    expect(looksLikePhone('Пётр 2')).toBe(false);
    expect(looksLikePhone('7+9000000001')).toBe(false);
    expect(parseNewAddress('+7 900 000-00-01')).toEqual({ kind: 'tg_phone', phone: '+7 900 000-00-01' });
    expect(parseNewAddress('@petr_ivanov')).toEqual({ kind: 'telegram', username: 'petr_ivanov' });
    expect(parseNewAddress('a@b.example')).toEqual({ kind: 'mail', address: 'a@b.example' });
  });

  it('номер -> «Новый адресат», подсказка «до 5 в сутки», `to` — строкой', async () => {
    const w = mount(NewMessageForm, { props: { projectId: null } });
    await typeTo(w, '+7 900 000-00-01');
    expect(w.get('.new-message-phone-hint').text()).toBe('по номеру телефона — до 5 в сутки');
    await w.get('.new-message-suggest-new').trigger('click');
    expect(w.get('.new-message-chip').text()).toContain('+7 900 000-00-01');
    expect(w.find('.new-message-subject').exists()).toBe(false);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(posted).toEqual([{ account_id: 1, project_id: null, to: '+7 900 000-00-01' }]);
    expect(w.emitted('done')).toEqual([[60]]);
    w.unmount();
  });

  it('tg_phone_daily = 0 — «поиск по номеру выключен», номер не предлагается', async () => {
    store.state.status = { accounts: [account({ tg_phone_daily: 0 })] } as unknown as StatusResponse;
    const w = mount(NewMessageForm, { props: { projectId: null } });
    await typeTo(w, '+79000000001');
    expect(w.get('.new-message-phone-hint').text()).toBe('поиск по номеру выключен');
    expect(w.find('.new-message-suggest-new').exists()).toBe(false);
    w.unmount();
  });

  it('аккаунты без Telegram: подсказки нет; @username по-прежнему работает', async () => {
    store.state.status = {
      accounts: [account({ id: 3, source: 'mail', tg_phone_daily: undefined, address: 'me@gmail.example' })],
    } as unknown as StatusResponse;
    const w = mount(NewMessageForm, { props: { projectId: null } });
    await typeTo(w, '+79000000001');
    expect(w.find('.new-message-phone-hint').exists()).toBe(false);
    w.unmount();

    store.state.status = { accounts: [account({})] } as unknown as StatusResponse;
    const w2 = mount(NewMessageForm, { props: { projectId: null } });
    await typeTo(w2, '@petr_ivanov');
    await w2.get('.new-message-suggest-new').trigger('click');
    await w2.get('form').trigger('submit');
    await flushPromises();
    expect(posted).toEqual([{ account_id: 1, project_id: null, to: '@petr_ivanov' }]);
    w2.unmount();
  });

  it('422 ядра показан как есть', async () => {
    const detail = 'to: номер телефона — в международном формате: +, затем 8–15 цифр';
    newResponse = () => jsonResponse({ detail }, 422);
    const w = mount(NewMessageForm, { props: { projectId: null } });
    await typeTo(w, '+79000000001');
    await w.get('.new-message-suggest-new').trigger('click');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.get('.new-message-error').text()).toContain('в международном формате');
    expect(w.emitted('done')).toBeUndefined();
    w.unmount();
  });
});
