// Метка источника беседы: значок в строке
// списка бесед, подсказка с полным названием; название не теряется.
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { Channel } from '../src/api/types';
import ChannelGroups from '../src/components/ChannelGroups.vue';

function ch(id: number, source: string | undefined): Channel {
  return {
    id, ext_id: `c${id}`, kind: 'dm', title: 'Анна Лим', tier: null, muted: false, archived: false, map_reason: null,
    last_msg_ts: 1000, my_msg_count: 0, first_seen_ts: null, unread: 2, project_id: 1, source,
  };
}

describe('метка источника в списке бесед', () => {
  it('лички одного человека — «SL», «MM», «TG», «@», подсказка с полным названием; название и счётчик на месте', () => {
    const w = mount(ChannelGroups, {
      props: { channels: [ch(1, 'slack'), ch(2, 'mattermost'), ch(3, 'telegram'), ch(4, 'mail')] },
    });
    const badges = w.findAll('.channel-button .source-badge');
    expect(badges.map((b) => b.text())).toEqual(['SL', 'MM', 'TG', '@']);
    expect(badges.map((b) => b.attributes('title'))).toEqual([
      'Источник: Slack', 'Источник: Mattermost', 'Источник: Telegram', 'Источник: Почта',
    ]);
    for (const row of w.findAll('.channel-button')) {
      expect(row.get('.channel-title').text()).toBe('Анна Лим');
      expect(row.find('.badge').exists()).toBe(true);
    }
  });

  it('источник неизвестен (старое ядро) — метки нет', () => {
    const w = mount(ChannelGroups, { props: { channels: [ch(1, undefined)] } });
    expect(w.find('.source-badge').exists()).toBe(false);
  });
});
