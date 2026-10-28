// Окно беседы на «Главной» компьютера (`ChannelModal`): клик по беседе в «Ждёт тебя» открывает окно, из списка слева
// — столбец; Esc / подложка / × закрывают и снимают активность; «На доску» — столбец; беседа уже столбцом — окна нет;
// телефон — прежний переход на экран беседы.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, Project } from '../src/api/types';
import ChannelModal from '../src/components/ChannelModal.vue';
import { boardState, openColumn, resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { layout } from '../src/util/layout';

const NOW = new Date(2026, 9, 9, 15, 0, 0).getTime();

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

function channel(): Channel {
  return {
    id: 283, ext_id: '283', tier: null, source: 'telegram', kind: 'dm', title: 'Покупатель', muted: false, archived: false, map_reason: null,
    last_msg_ts: NOW, my_msg_count: 0, first_seen_ts: NOW, unread: 0, project_id: 3,
  } as Channel;
}

const project: Project = { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true };

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const p = new URL(input, 'http://localhost').pathname;
    if (/\/messages$/.test(p)) return json({ messages: [], next_before: null });
    return json([]);
  }));
  vi.stubGlobal('IntersectionObserver', StubObserver);
  store.state.projects = [project];
  store.state.sync.channelsById[283] = channel();
  store.state.sync.messagesByChannel[283] = [];
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  store.state.channelModalId = null;
  store.state.boardActiveChannelId = null;
});

afterEach(() => {
  resetBoard();
  layout.mode = 'full';
  store.state.channelModalId = null;
  store.state.homeOpen = true;
  store.state.boardActiveChannelId = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('openChannelAt на главной компьютера', () => {
  it('по умолчанию — окно, столбец не открывается', async () => {
    await store.openChannelAt(3, 283);
    expect(store.state.channelModalId).toBe(283);
    expect(boardState.layout.open).toEqual([]);
    expect(store.state.homeOpen).toBe(true);
    expect(store.state.currentChannelId).toBeNull();
  });

  it('с { column: true } — столбец, окна нет', async () => {
    await store.openChannelAt(3, 283, {}, { column: true });
    expect(store.state.channelModalId).toBeNull();
    expect(boardState.layout.open).toEqual([{ kind: 'channel', id: 283 }]);
  });

  it('беседа уже столбцом — окна нет, столбец подсвечивается', async () => {
    openColumn({ kind: 'channel', id: 283 });
    const seq = boardState.focusSeq;
    await store.openChannelAt(3, 283);
    expect(store.state.channelModalId).toBeNull();
    expect(boardState.focusSeq).toBeGreaterThan(seq);
  });

  it('телефон — прежний переход на экран беседы', async () => {
    layout.mode = 'phone';
    await store.openChannelAt(3, 283);
    expect(store.state.channelModalId).toBeNull();
    expect(store.state.currentChannelId).toBe(283);
    expect(store.state.homeOpen).toBe(false);
  });

  it('список слева на доске просит столбец', async () => {
    // Контракт опции: вызов из списка идёт с { column: true } четвёртым аргументом.
    const text = await import('node:fs').then((fs) => fs.readFileSync('src/components/BoardList.vue', 'utf8'));
    expect(text).toContain('openChannelAt(projectId, channelId, {}, { column: true })');
  });
});

describe('ChannelModal', () => {
  async function open(): Promise<ReturnType<typeof mount>> {
    store.state.channelModalId = 283;
    const w = mount(ChannelModal, { props: { channelId: 283 }, attachTo: document.body });
    await flushPromises();
    return w;
  }

  it('диалог с названием, беседа активна пока окно открыто', async () => {
    const w = await open();
    const d = w.get('[role="dialog"]');
    expect(d.attributes('aria-modal')).toBe('true');
    expect(d.attributes('aria-label')).toBe('Беседа: Покупатель');
    expect(store.state.boardActiveChannelId).toBe(283);
    expect(w.find('.composer').exists()).toBe(true);
    w.unmount();
    expect(store.state.boardActiveChannelId).toBeNull();
  });

  it('Esc закрывает', async () => {
    const w = await open();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(store.state.channelModalId).toBeNull();
    w.unmount();
    expect(store.state.boardActiveChannelId).toBeNull();
  });

  it('клик по подложке закрывает, по самому окну — нет', async () => {
    const w = await open();
    await w.get('[role="dialog"]').trigger('click');
    expect(store.state.channelModalId).toBe(283);
    await w.get('.channel-modal-overlay').trigger('click');
    expect(store.state.channelModalId).toBeNull();
    w.unmount();
  });

  it('× закрывает', async () => {
    const w = await open();
    await w.get('.channel-modal-close').trigger('click');
    expect(store.state.channelModalId).toBeNull();
    w.unmount();
  });

  it('«На доску» открывает столбец и закрывает окно', async () => {
    const w = await open();
    await w.get('.channel-modal-menu-button').trigger('click');
    await w.get('.channel-modal-board').trigger('click');
    expect(store.state.channelModalId).toBeNull();
    expect(boardState.layout.open).toEqual([{ kind: 'channel', id: 283 }]);
    w.unmount();
  });

  it('уход с главной («Открыть в проекте») закрывает окно', async () => {
    const w = await open();
    await w.get('.channel-modal-menu-button').trigger('click');
    await w.get('.channel-modal-project').trigger('click');
    await flushPromises();
    expect(store.state.homeOpen).toBe(false);
    expect(store.state.channelModalId).toBeNull();
    w.unmount();
  });
});
