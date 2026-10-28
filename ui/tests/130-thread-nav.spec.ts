// Ф66-5: ответ в треде поднимает беседу (сортировка по last_activity_ts), метка «свежий тред» в строке,
// клик по беседе без нового в основной ленте и «Нужна реакция» с thread_ext_id открывают сразу панель треда.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, Project } from '../src/api/types';
import ChannelGroups from '../src/components/ChannelGroups.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { channelsOfProject } from '../src/store/selectors';
import { layout } from '../src/util/layout';
import { threadTargetOf } from '../src/util/threadNav';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

class StubObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

function ch(id: number, extra: Partial<Channel> = {}): Channel {
  return {
    id, ext_id: `c${id}`, kind: 'group', title: `Канал ${id}`, tier: null, muted: false, archived: false,
    map_reason: null, last_msg_ts: 1000, my_msg_count: 0, first_seen_ts: null, unread: 0, unread_top: 0,
    unread_threads: 0, project_id: 3, source: 'slack', ...extra,
  } as Channel;
}

const project: Project = { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true };

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  layout.mode = 'full';
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const p = new URL(input, 'http://localhost').pathname;
    if (/\/threads\/[^/]+\/messages$/.test(p)) return json({ messages: [], next_before: null, parent: null });
    if (/\/messages$/.test(p)) return json({ messages: [], next_before: null });
    return json([]);
  }));
  vi.stubGlobal('IntersectionObserver', StubObserver);
  store.state.projects = [project];
  store.state.sync.channelsById = {};
  store.state.homeOpen = false;
  store.state.openThread = null;
  store.state.currentChannelId = null;
  store.state.currentProjectId = 3;
});

afterEach(() => {
  resetBoard();
  store.state.openThread = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф66-5: подъём и метка', () => {
  it('сортировка по last_activity_ts: ответ в треде поднимает беседу выше более нового last_msg_ts', () => {
    store.state.sync.channelsById = {
      1: ch(1, { last_msg_ts: 5000 }),
      2: ch(2, { last_msg_ts: 1000, last_activity_ts: 9000 }),
      3: ch(3, { last_msg_ts: 3000 }),
    };
    expect(channelsOfProject(store.state.sync, 3).map((c) => c.id)).toEqual([2, 1, 3]);
  });

  it('метка «свежий тред» в строке; с бейджем 💬N — не дублируется', () => {
    const w = mount(ChannelGroups, {
      props: {
        channels: [
          ch(1, { fresh_threads: 1, fresh_thread_ext: 'P1' }),
          ch(2, { fresh_threads: 3, fresh_thread_ext: 'P2' }),
          ch(3, { fresh_threads: 1, fresh_thread_ext: 'P3', unread_threads: 1, unread: 1 }),
          ch(4),
        ],
      },
    });
    const labels = w.findAll('.badge-fresh-thread').map((b) => b.text());
    expect(labels).toEqual(['💬 свежий тред', '💬 3 тредов']);
    expect(w.findAll('.badge-threads')).toHaveLength(1);
  });
});

describe('Ф66-5: клик открывает тред', () => {
  it('threadTargetOf: только когда в основной ленте нового нет', () => {
    expect(threadTargetOf(ch(1, { fresh_thread_ext: 'P1' }))).toBe('P1');
    expect(threadTargetOf(ch(1, { fresh_thread_ext: 'P1', unread_top: 2, unread: 2 }))).toBeNull();
    expect(threadTargetOf(ch(1))).toBeNull();
  });

  it('клик по беседе со свежим тредом — открыт этот тред', async () => {
    store.state.sync.channelsById = { 1: ch(1, { fresh_threads: 1, fresh_thread_ext: 'P1' }) };
    const w = mount(ChannelGroups, { props: { channels: [store.state.sync.channelsById[1]] } });
    await w.get('.channel-button').trigger('click');
    await flushPromises();
    expect(store.state.currentChannelId).toBe(1);
    expect(store.state.openThread).toEqual({ channelId: 1, threadExtId: 'P1' });
  });

  it('клик по беседе с непрочитанным в основной ленте — тред не открывается', async () => {
    store.state.sync.channelsById = { 1: ch(1, { fresh_thread_ext: 'P1', unread_top: 1, unread: 1 }) };
    const w = mount(ChannelGroups, { props: { channels: [store.state.sync.channelsById[1]] } });
    await w.get('.channel-button').trigger('click');
    await flushPromises();
    expect(store.state.currentChannelId).toBe(1);
    expect(store.state.openThread).toBeNull();
  });

  it('«Нужна реакция» с thread_ext_id — беседа и тред, цель фокуса внутри треда', async () => {
    store.state.sync.channelsById = { 1: ch(1) };
    store.state.homeOpen = true;
    await store.openChannelAt(3, 1, { messageId: 77, threadExtId: 'P9' });
    await flushPromises();
    expect(store.state.currentChannelId).toBe(1);
    expect(store.state.openThread).toEqual({ channelId: 1, threadExtId: 'P9' });
    expect(store.state.focus).toMatchObject({ channelId: 1, messageId: 77, threadExtId: 'P9' });
  });

  it('«Нужна реакция» без thread_ext_id — тред не открывается', async () => {
    store.state.sync.channelsById = { 1: ch(1) };
    store.state.homeOpen = true;
    store.state.channelModalId = null;
    await store.openChannelAt(3, 1, { messageId: 77 });
    await flushPromises();
    expect(store.state.openThread).toBeNull();
  });
});
