// Ф12-1 C2 (`docs/architecture.md`): беседа-заготовка черновика новому адресату —
// пометка «новая» в списке, вместо поля ввода строка «Новая беседа: …», на карточке черновика — «кому: …».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, OutboxListItem } from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelView from '../src/components/ChannelView.vue';
import Composer from '../src/components/Composer.vue';
import DraftCard from '../src/components/DraftCard.vue';
import { store } from '../src/store/instance';
import { draftTargetLabel } from '../src/util/home';

const NEW_TO = { kind: 'tg_username' as const, username: 'Petr_Ivanov' };

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'new:0123456789abcdef0123456789abcdef',
    kind: 'dm',
    title: '@Petr_Ivanov',
    tier: null,
    muted: false,
    archived: false,
    map_reason: 'новая беседа',
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 1000,
    unread: 0,
    project_id: 1,
    ...overrides,
  };
}

function draft(overrides: Partial<OutboxListItem>): OutboxListItem {
  return {
    id: 7,
    channel_id: 1,
    project_id: 1,
    channel_title: '@Petr_Ivanov',
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
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sync.channelsById = {};
  store.state.sync.threads = {};
  store.state.openThread = null;
  store.state.currentProjectId = 1;
  store.state.currentChannelId = null;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
      return jsonResponse([]);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф12-1: беседа-заготовка в интерфейсе', () => {
  // Ф12-5 C3: строка — причина из ядра (`can_reply_reason`), а не зашитый текст Ф12-1; зашитый — только без причины.
  it('в беседе с new_to вместо поля ввода — причина из ядра, без ссылки в трекер', async () => {
    store.state.sync.channelsById[1] = channel({
      new_to: NEW_TO,
      can_reply: false,
      can_reply_reason: 'новая беседа — сначала уйдёт черновик',
    });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.findComponent(Composer).exists()).toBe(false);
    expect(wrapper.get('.no-reply').text()).toBe('новая беседа — сначала уйдёт черновик');
    expect(wrapper.find('a.no-reply-link').exists()).toBe(false);
    wrapper.unmount();
  });

  it('после привязки (new_to нет, can_reply снова true) — обычное поле ввода', async () => {
    store.state.sync.channelsById[1] = channel({ ext_id: '555', can_reply: true, title: 'Пётр Иванов' });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.findComponent(Composer).exists()).toBe(true);
    expect(wrapper.find('.no-reply').exists()).toBe(false);
    wrapper.unmount();
  });

  it('в списке проекта — пометка «новая» только у заготовки', async () => {
    store.state.sync.channelsById[1] = channel({ new_to: NEW_TO, can_reply: false });
    store.state.sync.channelsById[2] = channel({ id: 2, ext_id: '777', title: 'Старая личка', new_to: undefined });
    const wrapper = mount(ChannelList);
    await flushPromises();
    const buttons = wrapper.findAll('.channel-button');
    const fresh = buttons.find((b) => b.text().includes('@Petr_Ivanov'));
    const old = buttons.find((b) => b.text().includes('Старая личка'));
    expect(fresh?.find('.badge-new-channel').text()).toBe('новая');
    expect(old?.find('.badge-new-channel').exists()).toBe(false);
    wrapper.unmount();
  });

  it('карточка черновика — «кому: @username (новая беседа)» / «кому: адрес · тема»', () => {
    const tg = mount(DraftCard, { props: { item: draft({ new_to: NEW_TO }) } });
    expect(tg.get('.draft-card-target').text()).toBe('кому: @Petr_Ivanov (новая беседа)');
    tg.unmount();
    const mail = draft({
      source: 'mail',
      kind: 'mail_thread',
      new_to: { kind: 'email', to: 'client@example.com', subject: 'Подпись для ООО', from: null },
    });
    expect(draftTargetLabel(mail)).toBe('кому: client@example.com · Подпись для ООО');
    expect(draftTargetLabel(draft({}))).toBeNull();
  });
});
