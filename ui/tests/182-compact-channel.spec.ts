// Ф90-3: беседа компактно — шапка одной строкой (× · название · источник · «⋯»), «На доску»/«Открыть в проекте»/стадия/
// режим — в «⋯»; поле ввода одной строкой [📎][поле][🎤][➤], файл/«Из буфера»/голосовое — в меню 📎; подсказки
// Ctrl+Enter в потоке нет (только title); строки `board-channel-foot` нет.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel } from '../src/api/types';
import ChannelModal from '../src/components/ChannelModal.vue';
import Composer from '../src/components/Composer.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';

const NOW = new Date(2026, 9, 24, 15, 0, 0).getTime();

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
    last_msg_ts: NOW, my_msg_count: 0, first_seen_ts: NOW, unread: 0, project_id: null,
  } as Channel;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  vi.stubGlobal('fetch', vi.fn(async (input: string) => {
    const p = new URL(input, 'http://localhost').pathname;
    const body = /\/messages$/.test(p) ? { messages: [], next_before: null } : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  }));
  vi.stubGlobal('IntersectionObserver', StubObserver);
  store.state.sync.channelsById[283] = channel();
  store.state.sync.messagesByChannel[283] = [];
  store.state.homeOpen = true;
  store.state.channelModalId = 283;
});

afterEach(() => {
  resetBoard();
  store.state.channelModalId = null;
  vi.unstubAllGlobals();
});

describe('Ф90-3: беседа компактно', () => {
  it('шапка окна: только ×, название, источник и «⋯»; действия внутри меню', async () => {
    const w = mount(ChannelModal, { props: { channelId: 283 }, attachTo: document.body });
    await flushPromises();
    const head = w.get('.channel-modal-head');
    expect(head.find('.channel-modal-board').exists()).toBe(false);
    expect(head.find('.channel-modal-project').exists()).toBe(false);
    expect(head.find('.stage-chip').exists()).toBe(false);
    expect(head.find('.channel-modal-close').exists()).toBe(true);
    await head.get('.channel-modal-menu-button').trigger('click');
    expect(w.find('.channel-modal-menu .channel-modal-board').exists()).toBe(true);
    expect(w.find('.channel-modal-menu .channel-modal-project').exists()).toBe(true);
    w.unmount();
  });

  it('поле ввода: одна строка, меню 📎 (файл, голосовое), «Отправить» — ➤ с title, подсказки Ctrl+Enter в потоке нет', async () => {
    store.state.sync.channelsById[1] = { ...channel(), id: 1 };
    const w = mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
    expect(w.find('.composer-row textarea').attributes('rows')).toBe('1');
    expect(w.find('.composer-attach-menu').exists()).toBe(false);
    expect(w.find('.submit-hint').exists()).toBe(false);
    const send = w.get('button.composer-send');
    expect(send.text()).toBe('➤');
    expect(send.attributes('title')).toMatch(/Enter/);
    await w.get('.composer-attach').trigger('click');
    expect(w.find('.composer-attach-menu .composer-attach-file').exists()).toBe(true);
    expect(w.find('.composer-attach-menu .composer-record, .composer-attach-menu .voice-start').exists()).toBe(true);
    w.unmount();
  });
});
