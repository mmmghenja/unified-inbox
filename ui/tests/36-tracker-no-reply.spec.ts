// Ф10-5 B5 (`docs/architecture.md`, тесты B-T6, B-T7): беседа уведомлений трекера
// (`can_reply === false`) — вместо поля ввода строка «Ответ в трекер из хаба не уходит» и ссылка в трекер (только
// `https://`, `rel="noopener noreferrer"`), в ленте и в панели треда; `can_reply` нет (старое ядро) — поле как
// раньше. Карточка Trello (`kind='card'`) — в группе «Задачи».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel } from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelView from '../src/components/ChannelView.vue';
import Composer from '../src/components/Composer.vue';
import ThreadPanel from '../src/components/ThreadPanel.vue';
import { store } from '../src/store/instance';
import { groupOf } from '../src/util/channelGroup';

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'trello:c:Tr1CardA',
    kind: 'card',
    title: 'Продавцы маркетплейсов',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 1,
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

const TRELLO = { name: 'trello' as const, key: 'Tr1CardA', url: 'https://trello.com/c/Tr1CardA' };

describe('B-T6: беседа трекера — без поля ввода, со ссылкой в трекер', () => {
  it('can_reply=false -> нет Composer, есть строка и ссылка «Открыть в Trello»', async () => {
    store.state.sync.channelsById[1] = channel({ can_reply: false, tracker: TRELLO });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.findComponent(Composer).exists()).toBe(false);
    expect(wrapper.find('textarea').exists()).toBe(false);
    expect(wrapper.get('.no-reply').text()).toContain('Ответ в трекер из хаба не уходит');
    const link = wrapper.get('a.no-reply-link');
    expect(link.text()).toBe('Открыть в Trello');
    expect(link.attributes('href')).toBe('https://trello.com/c/Tr1CardA');
    expect(link.attributes('target')).toBe('_blank');
    expect(link.attributes('rel')).toBe('noopener noreferrer');
    wrapper.unmount();
  });

  it('Jira -> «Открыть в Jira»', async () => {
    store.state.sync.channelsById[1] = channel({
      kind: 'issue',
      can_reply: false,
      tracker: { name: 'jira', key: 'WEB-39', url: 'https://acme.atlassian.net/browse/WEB-39' },
    });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.get('a.no-reply-link').text()).toBe('Открыть в Jira');
    wrapper.unmount();
  });

  it('url не https -> ссылки нет, строка есть', async () => {
    store.state.sync.channelsById[1] = channel({
      can_reply: false,
      tracker: { name: 'trello', key: 'Tr1CardA', url: 'javascript:alert(1)' },
    });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.find('a').exists()).toBe(false);
    expect(wrapper.get('.no-reply').text()).toContain('Ответ в трекер из хаба не уходит');
    wrapper.unmount();
  });

  it('can_reply не задан (старое ядро) -> Composer есть, строки нет', async () => {
    store.state.sync.channelsById[1] = channel({ kind: 'mail_thread' });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.findComponent(Composer).exists()).toBe(true);
    expect(wrapper.find('.no-reply').exists()).toBe(false);
    wrapper.unmount();
  });

  it('панель треда беседы трекера — без поля ответа; обычной — с полем', async () => {
    store.state.sync.channelsById[1] = channel({ can_reply: false, tracker: TRELLO });
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    store.state.sync.threads['1:P1'] = { parent: null, replies: [], nextBefore: null, loading: false };
    const wrapper = mount(ThreadPanel);
    expect(wrapper.findComponent(Composer).exists()).toBe(false);
    expect(wrapper.get('a.no-reply-link').attributes('rel')).toBe('noopener noreferrer');
    wrapper.unmount();

    store.state.sync.channelsById[1] = channel({ can_reply: true, tracker: null });
    const plain = mount(ThreadPanel);
    expect(plain.findComponent(Composer).exists()).toBe(true);
    expect(plain.find('.no-reply').exists()).toBe(false);
    plain.unmount();
  });
});

describe('B-T7: карточка Trello — в группе «Задачи»', () => {
  it("kind='card' и 'issue' -> «Задачи»; в списке — под заголовком «Задачи»", async () => {
    expect(groupOf('card')).toBe('Задачи');
    expect(groupOf('issue')).toBe('Задачи');
    store.state.sync.channelsById = {
      1: channel({ id: 1, attention: 'react', can_reply: false, tracker: TRELLO }),
    };
    const wrapper = mount(ChannelList);
    await flushPromises();
    const react = wrapper.get('.attention-group--react');
    expect(react.findAll('.channel-group-title').map((h) => h.text())).toEqual(['Задачи']);
    expect(react.findAll('.channel-title').map((t) => t.text())).toEqual(['Продавцы маркетплейсов']);
    wrapper.unmount();
  });
});

describe('Ф84-1: беседа письма-уведомления по ключу (`tracker.name === notify`)', () => {
  it('ссылка «Открыть на сайте», поля ввода нет', async () => {
    store.state.sync.channelsById[1] = channel({
      ext_id: 'notify:fl:2000002',
      kind: 'task',
      can_reply: false,
      tracker: { name: 'notify', key: '2000002', url: 'https://www.jobs.example/messages/orders?dialogId=2000002' },
    });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.findComponent(Composer).exists()).toBe(false);
    const link = wrapper.get('a.no-reply-link');
    expect(link.text()).toBe('Открыть на сайте');
    expect(link.attributes('href')).toBe('https://www.jobs.example/messages/orders?dialogId=2000002');
    wrapper.unmount();
  });
});
