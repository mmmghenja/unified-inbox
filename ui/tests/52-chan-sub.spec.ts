// Ф14-5 D/П9 (`docs/architecture.md`: в шапке беседы под живой подпиской сессии —
// пометка «слушает сессия <имя или #pid>»; без подписки, у завершённой или мёртвой подписки, у подписки другой
// беседы — нет. Беседа сама спрашивает `GET /subs?live=1` при открытии и раз в 60 с.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, ChanSub } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import { store } from '../src/store/instance';

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: 7,
    ext_id: 'tg:7',
    kind: 'dm',
    title: 'Алиса',
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

function sub(overrides: Partial<ChanSub> = {}): ChanSub {
  return {
    id: 4,
    channel_id: 7,
    project_id: 3,
    session_name: 'МАРКЕТОЛОГ',
    session_pid: 100,
    live: true,
    listening: true,
    waiting: true,
    ended_ts: null,
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

let subsRows: ChanSub[] = [];
let subsCalls: string[] = [];

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sync.channelsById = { 7: channel(), 8: channel({ id: 8, title: 'Другая' }) };
  store.state.sync.threads = {};
  store.state.openThread = null;
  store.state.currentProjectId = 3;
  store.state.currentChannelId = 7;
  subsRows = [];
  subsCalls = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname.endsWith('/subs')) {
        subsCalls.push(url.search);
        return jsonResponse({ items: [], channels: subsRows });
      }
      if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
      return jsonResponse([]);
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф14-5 П9: пометка «слушает сессия …» в шапке беседы', () => {
  it('живая подписка этой беседы -> пометка с именем сессии', async () => {
    subsRows = [sub()];
    const wrapper = mount(ChannelView, { props: { channelId: 7 } });
    await flushPromises();
    expect(subsCalls).toEqual(['?live=1']);
    expect(wrapper.get('.chan-sub-mark').text()).toBe('слушает: МАРКЕТОЛОГ');
    wrapper.unmount();
  });

  it('сессия без имени -> #pid', async () => {
    subsRows = [sub({ session_name: null })];
    const wrapper = mount(ChannelView, { props: { channelId: 7 } });
    await flushPromises();
    expect(wrapper.get('.chan-sub-mark').text()).toBe('слушает: #100');
    wrapper.unmount();
  });

  it('без подписки, подписка другой беседы, завершённая или мёртвая -> пометки нет', async () => {
    for (const rows of [
      [],
      [sub({ channel_id: 8 })],
      [sub({ ended_ts: 5 })],
      [sub({ live: false })],
    ]) {
      subsRows = rows;
      const wrapper = mount(ChannelView, { props: { channelId: 7 } });
      await flushPromises();
      expect(wrapper.find('.chan-sub-mark').exists()).toBe(false);
      wrapper.unmount();
    }
  });

  it('смена беседы перезапрашивает подписки; опрос раз в 60 с, после закрытия — нет', async () => {
    vi.useFakeTimers();
    subsRows = [sub({ channel_id: 8 })];
    const wrapper = mount(ChannelView, { props: { channelId: 7 } });
    await flushPromises();
    expect(wrapper.find('.chan-sub-mark').exists()).toBe(false);
    await wrapper.setProps({ channelId: 8 });
    await flushPromises();
    expect(wrapper.get('.chan-sub-mark').text()).toBe('слушает: МАРКЕТОЛОГ');
    expect(subsCalls.length).toBe(2);
    subsRows = [];
    await vi.advanceTimersByTimeAsync(60_000);
    await flushPromises();
    expect(subsCalls.length).toBe(3);
    expect(wrapper.find('.chan-sub-mark').exists()).toBe(false);
    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(subsCalls.length).toBe(3);
  });

  it('сбой запроса — пометки нет, ошибки не видно', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        const url = new URL(input, 'http://localhost');
        if (url.pathname.endsWith('/subs')) return new Response('{"detail":"x"}', { status: 500 });
        if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
        return jsonResponse([]);
      }),
    );
    const wrapper = mount(ChannelView, { props: { channelId: 7 } });
    await flushPromises();
    expect(wrapper.find('.chan-sub-mark').exists()).toBe(false);
    wrapper.unmount();
  });
});
