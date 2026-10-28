// Ф8-3, интерфейс (`docs/architecture.md`, тесты T17–T18): выбор режима беседы в шапке
// шлёт `PATCH attention` («Авто» -> `null`), красный/серый счётчики в списках, присутствие («Владелец смотрит эту
// беседу»): POST сразу и раз в 20 с, смена беседы/треда — сразу новое, blur/скрытие — `channel_id: null`.
// T19 (уведомлений браузера больше нет) — `14-store-lifecycle.spec.ts` и `24-sync-resilience.spec.ts`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, Project } from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelView from '../src/components/ChannelView.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';
import { PRESENCE_HEARTBEAT_MS, PresenceTracker, type PresenceTarget } from '../src/store/presence';
import { createStore } from '../src/store/store';
import type { EventSourceLike } from '../src/store/stream';

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'c1',
    kind: 'group',
    title: 'фронт',
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

function project(overrides: Partial<Project>): Project {
  return { id: 1, name: 'Initech', color: null, sort: 0, muted: false, workdir: null, unread: 0, ...overrides };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function recordingFetch(): { sent: Sent[]; fetchMock: ReturnType<typeof vi.fn> } {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    sent.push({ method, path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.pathname === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (url.pathname === '/api/v1/status') {
      return jsonResponse({ accounts: [], dead_letter: {}, outbox_pending: 0, outbox_unknown: 0, max_seq: 1, now: 0 });
    }
    if (url.pathname === '/api/v1/projects') return jsonResponse([]);
    if (url.pathname === '/api/v1/auth/me') {
      return jsonResponse({ token_id: 1, kind: 'ui', scopes: ['read'], project_ids: null, csrf: 'c' });
    }
    if (url.pathname.startsWith('/api/v1/channels/') && url.pathname.endsWith('/messages')) {
      return jsonResponse({ messages: [], next_before: null });
    }
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sync.channelsById = {};
  store.state.currentProjectId = 1;
  store.state.currentChannelId = null;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// --- T17: выбор режима в шапке беседы ------------------------------------------------------------------------

describe('Ф8-3 T17: выбор режима беседы шлёт PATCH attention', () => {
  it('«Авто (сейчас: …)» по attention_auto; «Реагирую» -> attention: "react"; «Авто» -> null', async () => {
    const { sent, fetchMock } = recordingFetch();
    vi.stubGlobal('fetch', fetchMock);
    store.state.sync.channelsById[1] = channel({ attention: 'info', attention_manual: null, attention_auto: 'info' });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    const select = wrapper.get('select.attention-select');
    expect((select.element as HTMLSelectElement).value).toBe('auto');
    expect(select.findAll('option').map((o) => o.text())).toEqual([
      'Авто (сейчас: к сведению (упоминания будят))',
      'Реагирую',
      'К сведению (упоминания будят)',
    ]);

    await select.setValue('react');
    await flushPromises();
    const patches = () => sent.filter((r) => r.method === 'PATCH');
    expect(patches()).toEqual([{ method: 'PATCH', path: '/api/v1/channels/1', body: { attention: 'react' } }]);

    store.state.sync.channelsById[1] = channel({ attention: 'react', attention_manual: 'react', attention_auto: 'info' });
    await flushPromises();
    expect((select.element as HTMLSelectElement).value).toBe('react');
    await select.setValue('auto');
    await flushPromises();
    expect(patches().at(-1)).toEqual({ method: 'PATCH', path: '/api/v1/channels/1', body: { attention: null } });
    // кнопка «Заглушить» осталась
    expect(wrapper.findAll('button').some((b) => b.text() === 'Заглушить')).toBe(true);
  });
});

// --- D2: красный счётчик — требующее реакции, серый — прочее ---------------------------------------------------

describe('Ф8-3 D2: счётчики в списках каналов и проектов', () => {
  it('канал: unread_react>0 — красный с ним; иначе серый; поле не пришло — как раньше', () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread: 5, unread_top: 5, unread_react: 2 });
    store.state.sync.channelsById[2] = channel({ id: 2, title: 'б', unread: 3, unread_top: 3, unread_react: 0 });
    store.state.sync.channelsById[3] = channel({ id: 3, title: 'в', unread: 4, unread_top: 4 });
    const wrapper = mount(ChannelList);
    expect(wrapper.findAll('.badge-react').map((b) => b.text())).toEqual(['2']);
    expect(wrapper.findAll('.badge-quiet').map((b) => b.text())).toEqual(['3']);
    expect(wrapper.findAll('.badge-plain').map((b) => b.text())).toEqual(['4']);
  });

  // Ф10-6: левой колонки проектов (`ProjectList`) больше нет — счётчики на вкладках верхней полосы.
  it('вкладка проекта: unread_react>0 — красный, иначе счётчика нет (Ф20-4 A1: серого в полосе нет)', () => {
    store.state.projects = [
      project({ id: 1, unread: 9, unread_react: 1 }),
      project({ id: 2, name: 'Globex', unread: 4, unread_react: 0 }),
    ];
    const wrapper = mount(TopBar);
    const tabs = wrapper.find('nav.project-tabs');
    expect(tabs.findAll('.badge-react').map((b) => b.text())).toEqual(['1']);
    expect(tabs.findAll('.badge-quiet')).toEqual([]);
    wrapper.unmount();
    store.state.projects = [];
  });
});

// --- T18: присутствие ------------------------------------------------------------------------------------------

describe('Ф8-3 T18: присутствие — POST сразу и раз в 20 с, смена беседы сразу, blur -> null', () => {
  it('трекер: фокус + открытая беседа, удары сердца, тред, blur и снова фокус', async () => {
    vi.useFakeTimers();
    let active = true;
    let target: PresenceTarget | null = { channelId: 7, threadExtId: null };
    const posts: Array<[string, number | null, string | null]> = [];
    const tracker = new PresenceTracker({
      post: async (clientId, channelId, threadExtId) => {
        posts.push([clientId, channelId, threadExtId]);
      },
      target: () => target,
      isActive: () => active,
      clientId: 'tab-1',
    });
    tracker.start();
    expect(posts).toEqual([['tab-1', 7, null]]);
    await vi.advanceTimersByTimeAsync(PRESENCE_HEARTBEAT_MS);
    await vi.advanceTimersByTimeAsync(PRESENCE_HEARTBEAT_MS);
    expect(posts).toHaveLength(3);
    expect(posts.every(([, channelId]) => channelId === 7)).toBe(true);

    target = { channelId: 7, threadExtId: '1.0' }; // открыли панель треда
    tracker.update();
    expect(posts.at(-1)).toEqual(['tab-1', 7, '1.0']);
    target = { channelId: 8, threadExtId: null }; // сменили беседу
    tracker.update();
    expect(posts.at(-1)).toEqual(['tab-1', 8, null]);

    active = false;
    window.dispatchEvent(new Event('blur'));
    expect(posts.at(-1)).toEqual(['tab-1', null, null]);
    const count = posts.length;
    await vi.advanceTimersByTimeAsync(3 * PRESENCE_HEARTBEAT_MS);
    expect(posts).toHaveLength(count); // не в фокусе — ни одного удара сердца

    active = true;
    window.dispatchEvent(new Event('focus'));
    expect(posts.at(-1)).toEqual(['tab-1', 8, null]);
    target = null; // закрыли беседу
    tracker.update();
    expect(posts.at(-1)).toEqual(['tab-1', null, null]);
    tracker.stop();
  });

  it('трекер: ничего не открыто с самого начала — запросов нет', () => {
    const posts: unknown[] = [];
    const tracker = new PresenceTracker({
      post: async (...args) => {
        posts.push(args);
      },
      target: () => null,
      isActive: () => true,
    });
    tracker.start();
    expect(posts).toEqual([]);
    expect(tracker.clientId).not.toBe('');
    tracker.stop();
  });

  it('стор: открытие беседы и треда — POST /presence с client_id вкладки', async () => {
    const { sent, fetchMock } = recordingFetch();
    vi.stubGlobal('fetch', fetchMock);
    const noStream: EventSourceLike = { addEventListener: () => {}, close: () => {}, readyState: 1 };
    const local = createStore({ createEventSource: () => noStream, presenceActive: () => true });
    await local.bootstrap({ location: { hash: '', pathname: '/', search: '' }, history: { replaceState: () => {} } });
    await flushPromises();
    await local.openChannel(5);
    await local.openThread(5, '9.0');
    await flushPromises();
    const presence = sent.filter((r) => r.path === '/api/v1/presence').map((r) => r.body as Record<string, unknown>);
    expect(presence.map((b) => [b.channel_id, b.thread_ext_id])).toEqual([
      [5, null],
      [5, '9.0'],
    ]);
    expect(new Set(presence.map((b) => b.client_id)).size).toBe(1);
    local.closeThread();
    await flushPromises();
    expect(sent.filter((r) => r.path === '/api/v1/presence').at(-1)?.body).toMatchObject({
      channel_id: 5,
      thread_ext_id: null,
    });
    (local as unknown as { stopBackground(): void }).stopBackground();
    api.setUnauthorizedHandler(null);
  });
});
