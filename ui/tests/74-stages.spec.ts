// Ф34-1 A6: стадии бесед в UI — поля через applySync, смена в шапке (в т.ч. 422), чип, фильтр с памятью, блок «Стадии».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel } from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelView from '../src/components/ChannelView.vue';
import { applySync, createSyncState } from '../src/store/applySync';
import { store } from '../src/store/instance';
import { stageGroups } from '../src/util/stages';

function channel(id: number, overrides: Partial<Channel> = {}): Channel {
  return {
    id,
    ext_id: `tg:${id}`,
    kind: 'dm',
    title: `Беседа ${id}`,
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000 + id,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 3,
    attention: 'react',
    stage: null,
    stage_note: null,
    stage_ts: null,
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let patches: { url: string; body: unknown }[] = [];
let patchResponse: Response | null = null;

beforeEach(() => {
  window.localStorage.clear();
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sync.channelsById = {
    1: channel(1, { stage: 'Тёплый', stage_note: 'догон до 07.10', stage_ts: 50 }),
    2: channel(2, { stage: ' тёплый ', stage_ts: 90 }),
    3: channel(3, { stage: 'Отказ', stage_ts: 10 }),
    4: channel(4),
    5: channel(5, { stage: 'Архивный', archived: true, stage_ts: 1 }),
  };
  store.state.sync.threads = {};
  store.state.openThread = null;
  store.state.currentProjectId = 3;
  store.state.currentChannelId = null;
  patches = [];
  patchResponse = null;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      if (init?.method === 'PATCH') {
        patches.push({ url: url.pathname, body: JSON.parse(String(init.body)) });
        return patchResponse ?? json({ ok: true });
      }
      if (url.pathname.endsWith('/subs')) return json({ items: [], channels: [] });
      if (url.pathname.endsWith('/messages')) return json({ messages: [], next_before: null });
      return json([]);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф34-1: поля стадии и счёт', () => {
  it('applySync кладёт stage/stage_note/stage_ts и снимает их, когда ядро шлёт null', () => {
    const state = createSyncState();
    applySync(state, { messages: [], channels: [channel(1, { stage: 'Тёплый', stage_note: 'н', stage_ts: 5 })], max_seq: 1 });
    expect(state.channelsById[1]).toMatchObject({ stage: 'Тёплый', stage_note: 'н', stage_ts: 5 });
    applySync(state, { messages: [], channels: [channel(1)], max_seq: 2 });
    expect(state.channelsById[1]).toMatchObject({ stage: null, stage_note: null, stage_ts: null });
  });

  it('стадии считаются без регистра и пробелов, без архивных; написание — по самой свежей', () => {
    const groups = stageGroups(Object.values(store.state.sync.channelsById));
    expect(groups.map((g) => [g.label, g.count])).toEqual([
      ['тёплый', 2],
      ['Отказ', 1],
    ]);
  });
});

describe('Ф34-1: смена стадии в шапке', () => {
  it('выбор существующей стадии -> PATCH stage (без заметки)', async () => {
    store.state.currentChannelId = 4;
    const wrapper = mount(ChannelView, { props: { channelId: 4 } });
    await flushPromises();
    await wrapper.get('select[aria-label="Стадия беседы"]').setValue('отказ');
    await flushPromises();
    expect(patches).toEqual([{ url: '/api/v1/channels/4', body: { stage: 'Отказ' } }]);
    wrapper.unmount();
  });

  it('беседа без проекта: в выпадающем — стадии других бесед без проекта', async () => {
    store.state.sync.channelsById = {
      6: channel(6, { project_id: null, stage: 'Звонок', stage_ts: 5 }),
      7: channel(7, { project_id: null }),
    };
    store.state.currentChannelId = 7;
    const wrapper = mount(ChannelView, { props: { channelId: 7 } });
    await flushPromises();
    await wrapper.get('select[aria-label="Стадия беседы"]').setValue('звонок');
    await flushPromises();
    expect(patches).toEqual([{ url: '/api/v1/channels/7', body: { stage: 'Звонок' } }]);
    wrapper.unmount();
  });

  it('«Новая…» -> ввод -> PATCH с обрезанным текстом; «Снять» -> stage null', async () => {
    store.state.currentChannelId = 4;
    const wrapper = mount(ChannelView, { props: { channelId: 4 } });
    await flushPromises();
    await wrapper.get('select[aria-label="Стадия беседы"]').setValue('__new__');
    await wrapper.get('input[aria-label="Новая стадия"]').setValue('  Договор  ');
    await wrapper.get('.stage-form').trigger('submit');
    await flushPromises();
    expect(patches[0].body).toEqual({ stage: 'Договор' });
    wrapper.unmount();

    store.state.currentChannelId = 3;
    const w2 = mount(ChannelView, { props: { channelId: 3 } });
    await flushPromises();
    await w2.get('select[aria-label="Стадия беседы"]').setValue('__clear__');
    await flushPromises();
    expect(patches[1]).toEqual({ url: '/api/v1/channels/3', body: { stage: null } });
    w2.unmount();
  });

  it('заметка: клик -> ввод -> PATCH stage_note; пустая -> null', async () => {
    store.state.currentChannelId = 1;
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.get('.stage-note').text()).toBe('догон до 07.10');
    await wrapper.get('.stage-note').trigger('click');
    await wrapper.get('input[aria-label="Заметка к стадии"]').setValue('');
    await wrapper.get('.stage-form').trigger('submit');
    await flushPromises();
    expect(patches).toEqual([{ url: '/api/v1/channels/1', body: { stage_note: null } }]);
    wrapper.unmount();
  });

  it('422 от ядра -> текст ошибки в шапке, поле ввода остаётся', async () => {
    patchResponse = json({ detail: 'стадия длиннее 40 символов' }, 422);
    store.state.currentChannelId = 4;
    const wrapper = mount(ChannelView, { props: { channelId: 4 } });
    await flushPromises();
    await wrapper.get('select[aria-label="Стадия беседы"]').setValue('__new__');
    await wrapper.get('input[aria-label="Новая стадия"]').setValue('очень длинная');
    await wrapper.get('.stage-form').trigger('submit');
    await flushPromises();
    expect(wrapper.get('.stage-error').text()).toBe('стадия длиннее 40 символов');
    expect(wrapper.find('.stage-form').exists()).toBe(true);
    wrapper.unmount();
  });
});

describe('Ф34-1: список бесед проекта', () => {
  it('чип стадии в строке беседы; у беседы без стадии чипа нет', async () => {
    const wrapper = mount(ChannelList);
    await flushPromises();
    expect(wrapper.get('.channel-button[data-channel-id="3"] .stage-chip').text()).toBe('Отказ');
    expect(wrapper.find('.channel-button[data-channel-id="4"] .stage-chip').exists()).toBe(false);
    wrapper.unmount();
  });

  it('фильтр: стадия / без стадии / все; выбор помнится и возвращается при новом монтировании', async () => {
    const wrapper = mount(ChannelList);
    await flushPromises();
    const ids = () => wrapper.findAll('.channel-button').map((b) => b.attributes('data-channel-id'));
    const select = wrapper.get('select[aria-label="Фильтр по стадии"]');
    await select.setValue('s:тёплый');
    expect(ids().sort()).toEqual(['1', '2']);
    await select.setValue('none');
    expect(ids()).toEqual(['4']);
    wrapper.unmount();

    const again = mount(ChannelList);
    await flushPromises();
    expect(again.findAll('.channel-button').map((b) => b.attributes('data-channel-id'))).toEqual(['4']);
    await again.get('select[aria-label="Фильтр по стадии"]').setValue('all');
    expect(again.findAll('.channel-button').length).toBe(4);
    again.unmount();
  });

  it('запомненная стадия, которой в проекте уже нет, не прячет список', async () => {
    window.localStorage.setItem('hub.stageFilter.3', 's:исчезла');
    const wrapper = mount(ChannelList);
    await flushPromises();
    expect(wrapper.findAll('.channel-button').length).toBe(4);
    wrapper.unmount();
  });

  it('блок «Стадии»: стадия -> число -> раскрытие -> клик открывает беседу; обновляется по sync', async () => {
    const open = vi.spyOn(store, 'openChannelOverlay').mockResolvedValue(undefined as never);
    const wrapper = mount(ChannelList);
    await flushPromises();
    const rows = wrapper.findAll('.stages-row');
    expect(rows.map((r) => r.text().replace(/\s+/g, ''))).toEqual(['▸тёплый2', '▸Отказ1']);
    await rows[0].trigger('click');
    await wrapper.get('.stages-channel[data-channel-id="1"]').trigger('click');
    expect(open).toHaveBeenCalledWith(1);
    applySync(store.state.sync, { messages: [], channels: [channel(4, { stage: 'Отказ', stage_ts: 99 })], max_seq: 9 });
    await flushPromises();
    expect(wrapper.get('.stages-row[data-stage-key="отказ"]').text().replace(/\s+/g, '')).toBe('▸Отказ2');
    wrapper.unmount();
  });

  it('в проекте нет стадий -> ни блока, ни фильтра', async () => {
    store.state.sync.channelsById = { 4: channel(4) };
    const wrapper = mount(ChannelList);
    await flushPromises();
    expect(wrapper.find('.stages-block').exists()).toBe(false);
    expect(wrapper.find('.stage-filter').exists()).toBe(false);
    wrapper.unmount();
  });
});
