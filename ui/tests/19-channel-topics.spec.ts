// Спека 1d-1, п.5: в списке каналов топик (`kind='topic'`) подписан «<форум> › <тема>» (`parent_title` из
// `GET /projects/{id}/channels`, см. `src/hub/core/queries.py::list_channels`). Здесь — только показ на
// стороне UI (ChannelList.vue); скрытие форума с дочерними топиками и сам `parent_title` — ответственность
// ядра (см. `tests/api/test_channel_topics.py`).
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';

import type { Channel } from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
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

describe('19 ChannelList: подпись топика «<форум> › <тема>»', () => {
  beforeEach(() => {
    store.state.sync.channelsById = {};
    store.state.currentProjectId = 1;
    store.state.currentChannelId = null;
  });

  it('топик с parent_title показывается как «форум › тема»', () => {
    store.state.sync.channelsById[1] = channel({
      id: 1,
      kind: 'topic',
      title: 'Обсуждение багов',
      parent_title: 'Мой форум',
    });
    const wrapper = mount(ChannelList);
    expect(wrapper.find('.channel-title').text()).toBe('Мой форум › Обсуждение багов');
  });

  it('обычный канал (не топик) показывает просто своё название, без форума', () => {
    store.state.sync.channelsById[2] = channel({ id: 2, kind: 'dm', title: 'Обычный чат', parent_title: null });
    const wrapper = mount(ChannelList);
    expect(wrapper.find('.channel-title').text()).toBe('Обычный чат');
  });

  it('топик без parent_title (например, ещё не пришёл в /sync) — просто своё название, без падения', () => {
    store.state.sync.channelsById[3] = channel({ id: 3, kind: 'topic', title: 'Тема без родителя' });
    const wrapper = mount(ChannelList);
    expect(wrapper.find('.channel-title').text()).toBe('Тема без родителя');
  });
});
