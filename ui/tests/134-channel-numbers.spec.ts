// Владелец 18.10: номера бесед и напоминаний (#id) видны в интерфейсе; беседу можно найти по номеру.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel } from '../src/api/types';
import ChannelGroups from '../src/components/ChannelGroups.vue';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelNo from '../src/components/ChannelNo.vue';
import ReminderCard from '../src/components/ReminderCard.vue';
import { store } from '../src/store/instance';

afterEach(() => vi.restoreAllMocks());

const ch = (id: number, over: Partial<Channel> = {}) =>
  ({ id, title: `Беседа ${id}`, kind: 'group', unread: 0, archived: false, muted: false, ...over }) as unknown as Channel;

describe('номер беседы', () => {
  it('ChannelNo: #id', () => {
    expect(mount(ChannelNo, { props: { id: 1099511627837 } }).text()).toBe('#1099511627837');
  });

  it('строка списка несёт номер', () => {
    const w = mount(ChannelGroups, { props: { channels: [ch(20)] } });
    expect(w.get('.channel-button .chan-no').text()).toBe('#20');
  });

  it('поиск по номеру: беседа не из загруженных — GET /channels/{id}, архив с пометкой', async () => {
    const spy = vi.spyOn(api, 'getChannel').mockResolvedValue(ch(777, { archived: true }));
    const open = vi.spyOn(store, 'openChannelInProject').mockResolvedValue(undefined);
    const w = mount(ChannelList);
    await w.get('.number-search-input').setValue('#777');
    await flushPromises();
    expect(spy).toHaveBeenCalledWith(777);
    const hit = w.get('[data-test="channel-no-result"]');
    expect(hit.text()).toContain('#777');
    expect(hit.text()).toContain('архив');
    await hit.trigger('click');
    expect(open).toHaveBeenCalledWith(777);
  });

  it('поиск по номеру: нет такой беседы — сообщение', async () => {
    vi.spyOn(api, 'getChannel').mockRejectedValue(new Error('404'));
    const w = mount(ChannelList);
    await w.get('.number-search-input').setValue('555');
    await flushPromises();
    expect(w.text()).toContain('Беседы с таким номером нет');
  });
});

describe('номер напоминания', () => {
  it('карточка напоминания: #id', () => {
    const reminder = { id: 38, channel_id: 20, due_ts: 0, note: null, created_by: 'x' } as never;
    const w = mount(ReminderCard, { props: { reminder } });
    expect(w.get('.reminder-no').text()).toBe('#38');
  });
});
