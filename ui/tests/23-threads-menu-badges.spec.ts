// Спека `docs/architecture.md`. Группа 6: бейджи канала (`unread_top` + «💬K»). Группа 7:
// кнопка «Треды» в шапке канала (видна при `unread_threads > 0`, список из API, `parent: null` -> заглушка,
// клик открывает тред).
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Channel, ThreadListItem } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import ChannelList from '../src/components/ChannelList.vue';
import ThreadsMenu from '../src/components/ThreadsMenu.vue';
import { store } from '../src/store/instance';

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'c1',
    kind: 'dm',
    title: 'Канал',
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

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.currentProjectId = 1;
  store.state.currentChannelId = null;
});

describe('23 группа 6: бейджи канала — unread_top + «💬K»', () => {
  it('unread_top — основное число, unread_threads>0 — второй значок «💬K»', () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread: 5, unread_top: 3, unread_threads: 2 });
    const wrapper = mount(ChannelList);
    expect(wrapper.find('.badge').text()).toBe('3');
    expect(wrapper.find('.badge-threads').text()).toBe('💬2');
  });

  it('unread_threads=0 — второго значка нет', () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread: 3, unread_top: 3, unread_threads: 0 });
    const wrapper = mount(ChannelList);
    expect(wrapper.find('.badge-threads').exists()).toBe(false);
  });

  it('поля не пришли (старое ядро) — как раньше, по unread целиком, без второго значка', () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread: 7 });
    const wrapper = mount(ChannelList);
    expect(wrapper.find('.badge').text()).toBe('7');
    expect(wrapper.find('.badge-threads').exists()).toBe(false);
  });

  it('unread_top=0 (весь unread — только треды) — основного бейджа нет, тредовый есть', () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread: 2, unread_top: 0, unread_threads: 2 });
    const wrapper = mount(ChannelList);
    expect(wrapper.find('.badge:not(.badge-threads)').exists()).toBe(false);
    expect(wrapper.find('.badge-threads').text()).toBe('💬2');
  });
});

describe('23 группа 7: кнопка «Треды» в шапке канала — видимость', () => {
  it('unread_threads=0 -> кнопки нет', () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread_threads: 0 });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    expect(wrapper.find('.threads-button').exists()).toBe(false);
  });

  it('unread_threads>0 -> кнопка видна, с числом', () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread_threads: 4 });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    expect(wrapper.find('.threads-button').text()).toContain('4');
  });
});

describe('23 группа 7: кнопка «Треды» — список из API, заглушка parent, клик открывает тред', () => {
  it('открытие показывает список из store.listThreads(channelId, {unread:true}), parent:null -> заглушка', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread_threads: 2 });
    const items: ThreadListItem[] = [
      {
        thread_ext_id: 'P1',
        thread: { reply_count: 3, source_reply_count: null, last_reply_ts: 1000, unread: 2, subscribed: true },
        parent: null,
      },
    ];
    const listSpy = vi.spyOn(store, 'listThreads').mockResolvedValue(items);

    const wrapper = mount(ThreadsMenu, { props: { channelId: 1 } });
    await wrapper.find('.threads-button').trigger('click');
    await new Promise((resolve) => setTimeout(resolve, 0));
    await wrapper.vm.$nextTick();

    expect(listSpy).toHaveBeenCalledWith(1, { unread: true });
    expect(wrapper.text()).toContain('(сообщение не загружено)');
  });

  it('клик по строке треда -> store.openThread(channelId, thread_ext_id)', async () => {
    store.state.sync.channelsById[1] = channel({ id: 1, unread_threads: 1 });
    const items: ThreadListItem[] = [
      {
        thread_ext_id: 'P2',
        thread: { reply_count: 1, source_reply_count: null, last_reply_ts: 1000, unread: 1, subscribed: true },
        parent: null,
      },
    ];
    vi.spyOn(store, 'listThreads').mockResolvedValue(items);
    const openSpy = vi.spyOn(store, 'openThread').mockResolvedValue();

    const wrapper = mount(ThreadsMenu, { props: { channelId: 1 } });
    await wrapper.find('.threads-button').trigger('click');
    await new Promise((resolve) => setTimeout(resolve, 0));
    await wrapper.vm.$nextTick();

    await wrapper.find('.thread-item').trigger('click');
    expect(openSpy).toHaveBeenCalledWith(1, 'P2');
  });
});
