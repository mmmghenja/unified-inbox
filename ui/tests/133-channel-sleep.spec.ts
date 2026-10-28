// Ф70-1 (`docs/architecture.md`): канал спит вместе с днём проекта — серая пометка
// «канал спит — день проекта закрыт» в шапке беседы спящего аккаунта и у аккаунта в «Раскладке»; поле ввода
// беседы на месте (ручная отправка уходит).
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Account, Channel, MapRule } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import MappingPanel from '../src/components/MappingPanel.vue';
import { store } from '../src/store/instance';
import { accountAsleep, CHANNEL_SLEEP_NOTE } from '../src/util/workday';

enableAutoUnmount(afterEach);

function account(id: number, sleep: Account['sleep'] = null): Account {
  return { id, source: 'slack', title: `акк ${id}`, status: 'ok', last_ok_ts: null, last_error: null, flood_until: null, sleep };
}

function channel(over: Partial<Channel>): Channel {
  return {
    id: 1, ext_id: 'c1', kind: 'group', title: 'общий', tier: null, muted: false, archived: false, map_reason: null,
    last_msg_ts: 1000, my_msg_count: 0, first_seen_ts: null, unread: 0, project_id: 1, source: 'slack',
    account_id: 2, ...over,
  };
}

function rule(accountId: number): MapRule {
  return { id: accountId, account_id: accountId, project_id: 1, priority: -100, kind: 'all', value: null, description: 'весь аккаунт', channels: 3 };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function setStatus(accounts: Account[]): void {
  store.state.status = { accounts, dead_letter: {}, outbox_pending: 0, outbox_approval: 0, outbox_unknown: 0, max_seq: 1, now: 0 };
}

beforeEach(() => {
  api.setCsrf('c');
  store.state.sync.channelsById = {};
  store.state.projects = [{ id: 1, name: 'Globex', color: null, sort: 0, muted: false, workdir: null, unread: 0 }];
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost');
    if (url.pathname === '/api/v1/map-rules') return jsonResponse([rule(2), rule(3)]);
    if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
    return jsonResponse([]);
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  store.state.status = null;
});

describe('Ф70-1: признак сна аккаунта', () => {
  it('accountAsleep — только аккаунт с непустым sleep', () => {
    const accounts = [account(2, { project_id: 1, since_ts: 5 }), account(3)];
    expect(accountAsleep(accounts, 2)).toBe(true);
    expect(accountAsleep(accounts, 3)).toBe(false);
    expect(accountAsleep(accounts, 9)).toBe(false);
    expect(accountAsleep(accounts, undefined)).toBe(false);
    expect(accountAsleep(undefined, 2)).toBe(false);
  });
});

describe('Ф70-1: пометка в шапке беседы', () => {
  it('спящий аккаунт — серая пометка, поле ввода на месте; не спит — пометки нет', async () => {
    setStatus([account(2, { project_id: 1, since_ts: 5 })]);
    store.state.sync.channelsById[1] = channel({});
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.get('[data-test="channel-sleep"]').text()).toBe(CHANNEL_SLEEP_NOTE);
    expect(wrapper.find('textarea').exists()).toBe(true);

    setStatus([account(2)]);
    await flushPromises();
    expect(wrapper.find('[data-test="channel-sleep"]').exists()).toBe(false);
  });
});

describe('Ф70-1: пометка у аккаунта в «Раскладке»', () => {
  it('только у спящего аккаунта', async () => {
    setStatus([account(2, { project_id: 1, since_ts: 5 }), account(3)]);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const marks = wrapper.findAll('[data-test="account-sleep"]');
    expect(marks.map((m) => m.text())).toEqual([CHANNEL_SLEEP_NOTE]);
    expect(wrapper.get('.mapping-account[data-account-id="2"]').find('[data-test="account-sleep"]').exists()).toBe(true);
  });
});
