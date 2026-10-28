// Ф23-1 (`docs/architecture.md`): лента с конца — страница 30, держаться низа наблюдением за
// высотой, пока владелец сам не прокрутил вверх; программная прокрутка `stick` не снимает; переоткрытие — последние 30.
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Message } from '../src/api/types';
import MessageFeed from '../src/components/MessageFeed.vue';
import ThreadPanel from '../src/components/ThreadPanel.vue';
import { store } from '../src/store/instance';
import { MESSAGE_PAGE } from '../src/store/store';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 7,
    project_id: 1,
    ext_id: '1',
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'текст',
    entities_json: null,
    attachments_json: null,
    forward_json: null,
    mentions_me: false,
    refs_json: null,
    reactions_json: null,
    ts: 1000,
    edited_ts: null,
    deleted: false,
    outbox_id: null,
    outbox_status: null,
    sender: null,
    ...overrides,
  };
}

class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  callback: () => void;
  targets: Element[] = [];
  constructor(callback: () => void) {
    this.callback = callback;
    FakeResizeObserver.instances.push(this);
  }
  observe(target: Element): void {
    this.targets.push(target);
  }
  unobserve(): void {}
  disconnect(): void {
    this.targets = [];
  }
  static fire(): void {
    for (const o of FakeResizeObserver.instances) if (o.targets.length > 0) o.callback();
  }
}

/** Геометрия ленты в jsdom (все размеры 0): прокручиваемые `scrollTop`/`scrollHeight`/`clientHeight`. */
function fakeGeometry(el: Element, height: number, client = 600): { grow(h: number): void } {
  let scrollHeight = height;
  let scrollTop = 0;
  Object.defineProperty(el, 'scrollHeight', { get: () => scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: client, configurable: true });
  Object.defineProperty(el, 'scrollTop', {
    get: () => scrollTop,
    set: (v: number) => {
      scrollTop = v;
    },
    configurable: true,
  });
  return {
    grow(h: number) {
      scrollHeight = h;
    },
  };
}

function manyMessages(channelId: number, n: number): Message[] {
  return Array.from({ length: n }, (_, i) =>
    msg({ id: i + 1, ext_id: String(i + 1), channel_id: channelId, ts: 1000 + i }),
  );
}

describe('Ф23-1: лента держится низа', () => {
  beforeEach(() => {
    FakeResizeObserver.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    vi.spyOn(store, 'loadOlderMessages').mockResolvedValue();
    store.state.sync.messagesByChannel[7] = manyMessages(7, 5);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete store.state.sync.messagesByChannel[7];
  });

  async function mountFeed() {
    const wrapper = mount(MessageFeed, { props: { channelId: 7 }, attachTo: document.body });
    const el = wrapper.get('.message-feed').element as HTMLElement;
    const geo = fakeGeometry(el, 2000);
    await vi.advanceTimersByTimeAsync(0);
    return { wrapper, el, geo };
  }

  it('открытие: низ; картинки догрузились (рост высоты) — снова низ', async () => {
    const { wrapper, el, geo } = await mountFeed();
    expect(el.scrollTop).toBe(2000);
    geo.grow(2600);
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(2600);
    geo.grow(3100);
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(3100);
    wrapper.unmount();
  });

  it('наблюдатель висит на содержимом ленты (ul)', async () => {
    const { wrapper } = await mountFeed();
    const targets = FakeResizeObserver.instances.flatMap((o) => o.targets);
    expect(targets.some((t) => t.tagName === 'UL')).toBe(true);
    wrapper.unmount();
  });

  it('владелец прокрутил вверх — рост высоты низ не тянет; вернулся к низу — держится снова', async () => {
    const { wrapper, el, geo } = await mountFeed();
    await vi.advanceTimersByTimeAsync(500); // окно «наша прокрутка» закрылось
    el.scrollTop = 300;
    el.dispatchEvent(new Event('scroll'));
    geo.grow(2600);
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(300);

    el.scrollTop = 2600 - 600 - 10; // у низа (в пределах 80 px)
    el.dispatchEvent(new Event('scroll'));
    geo.grow(3000);
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(3000);
    wrapper.unmount();
  });

  it('наша прокрутка (событие scroll в окне после неё) stick не снимает', async () => {
    const { wrapper, el, geo } = await mountFeed();
    // высота выросла ещё до прихода scroll-события нашей прокрутки: по геометрии «не у низа», но это не владелец
    geo.grow(4000);
    el.dispatchEvent(new Event('scroll'));
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(4000);
    wrapper.unmount();
  });

  it('компенсация догрузки вверх не снимает stick и не прыгает', async () => {
    const { wrapper, el, geo } = await mountFeed();
    await vi.advanceTimersByTimeAsync(500);
    el.scrollTop = 10; // владелец у самого верха -> догрузка
    el.dispatchEvent(new Event('scroll'));
    expect(store.loadOlderMessages).toHaveBeenCalledWith(7);
    geo.grow(2000 + 900); // вставили старые сверху
    await vi.advanceTimersByTimeAsync(0);
    expect(el.scrollTop).toBe(10 + 900); // позиция сохранена
    FakeResizeObserver.fire(); // рост высоты — низ НЕ тянет (владелец наверху)
    expect(el.scrollTop).toBe(910);
    wrapper.unmount();
  });
});

describe('Ф23-1: страница 30 и переоткрытие', () => {
  beforeEach(() => {
    store.state.sync.messagesByChannel = {};
    store.state.nextBefore = {};
    store.state.focus = null;
  });
  afterEach(() => {
    vi.restoreAllMocks();
    store.state.sync.messagesByChannel = {};
    store.state.nextBefore = {};
  });

  it('openChannel и loadOlderMessages просят 30', async () => {
    const spy = vi
      .spyOn(api, 'listMessages')
      .mockResolvedValue({ messages: manyMessages(9, 3), next_before: '1,0,1' } as never);
    await store.openChannel(9);
    expect(spy).toHaveBeenCalledWith(9, { limit: MESSAGE_PAGE });
    expect(MESSAGE_PAGE).toBe(30);
    await store.loadOlderMessages(9);
    expect(spy).toHaveBeenLastCalledWith(9, { before: '1,0,1', limit: 30 });
  });

  it('переоткрытие: от накопленных 120 остаются последние 30, курсор — из свежей страницы', async () => {
    store.state.sync.messagesByChannel[9] = manyMessages(9, 120);
    store.state.nextBefore[9] = '1000,0,1';
    const fresh = manyMessages(9, 120).slice(90);
    let during: number | null = null;
    vi.spyOn(api, 'listMessages').mockImplementation(async () => {
      during = store.state.sync.messagesByChannel[9]!.length;
      return { messages: fresh, next_before: '1090,0,91' } as never;
    });
    await store.openChannel(9);
    expect(during).toBe(30);
    expect(store.state.sync.messagesByChannel[9]).toHaveLength(30);
    expect(store.state.sync.messagesByChannel[9]![0]!.id).toBe(91);
    expect(store.state.nextBefore[9]).toBe('1090,0,91');
  });

  it('цель фокуса глубже последних 30 — не режем', async () => {
    store.state.sync.messagesByChannel[9] = manyMessages(9, 120);
    store.state.focus = { channelId: 9, messageId: 5, outboxId: null, seq: 1 };
    vi.spyOn(api, 'listMessages').mockResolvedValue({ messages: manyMessages(9, 120).slice(90), next_before: null } as never);
    await store.openChannel(9);
    expect(store.state.sync.messagesByChannel[9]).toHaveLength(120);
  });
});

describe('Ф23-1: тред', () => {
  beforeEach(() => {
    FakeResizeObserver.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    store.state.openThread = null;
    store.state.sync.threads = {};
  });

  it('openThread и догрузка ответов просят 30', async () => {
    vi.useRealTimers();
    const spy = vi
      .spyOn(api, 'listThreadMessages')
      .mockResolvedValue({ messages: [], next_before: '1,0,1', parent: null } as never);
    await store.openThread(1, 'P1');
    expect(spy).toHaveBeenCalledWith(1, 'P1', { limit: 30 });
    await store.loadOlderReplies();
    expect(spy).toHaveBeenLastCalledWith(1, 'P1', { before: '1,0,1', limit: 30 });
  });

  it('панель треда держит низ при росте высоты и отпускает, когда владелец ушёл вверх', async () => {
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    store.state.sync.threads['1:P1'] = {
      parent: msg({ id: 1, ext_id: 'P1', channel_id: 1 }),
      replies: [msg({ id: 2, ext_id: 'R1', channel_id: 1, thread_ext_id: 'P1' })],
      nextBefore: null,
      loading: false,
    };
    const wrapper = mount(ThreadPanel, { attachTo: document.body });
    const el = wrapper.get('.thread-messages').element as HTMLElement;
    const geo = fakeGeometry(el, 1500);
    await vi.advanceTimersByTimeAsync(0);
    expect(el.scrollTop).toBe(1500);
    geo.grow(2200);
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(2200);

    await vi.advanceTimersByTimeAsync(500);
    el.scrollTop = 200;
    el.dispatchEvent(new Event('scroll'));
    geo.grow(2800);
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(200);
    wrapper.unmount();
  });
});

describe('Сжатие контейнера прокрутки', () => {
  beforeEach(() => {
    FakeResizeObserver.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    vi.spyOn(store, 'loadOlderMessages').mockResolvedValue();
    store.state.sync.messagesByChannel[7] = manyMessages(7, 5);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete store.state.sync.messagesByChannel[7];
  });

  it('сам контейнер под наблюдением; при stick сжатие прижимает к низу, при снятом — нет', async () => {
    const wrapper = mount(MessageFeed, { props: { channelId: 7 }, attachTo: document.body });
    const el = wrapper.get('.message-feed').element as HTMLElement;
    const geo = fakeGeometry(el, 2000);
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeResizeObserver.instances.some((o) => o.targets.includes(el))).toBe(true);
    geo.grow(2100); // карточка черновика сжала контейнер: высота прокрутки изменилась
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(2100);
    await vi.advanceTimersByTimeAsync(500);
    el.scrollTop = 100;
    el.dispatchEvent(new Event('scroll')); // владелец ушёл вверх — stick снят
    geo.grow(2300);
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(100);
    wrapper.unmount();
  });

  it('цель-черновик без строки в ленте снимается со стора', async () => {
    store.state.focus = { channelId: 7, messageId: null, outboxId: 999, seq: 1 };
    const wrapper = mount(MessageFeed, { props: { channelId: 7 }, attachTo: document.body });
    await vi.advanceTimersByTimeAsync(500);
    expect(store.state.focus).toBeNull();
    wrapper.unmount();
  });
});

describe('Фокус с главной: низ держится, цель глубже — не уезжает', () => {
  beforeEach(() => {
    FakeResizeObserver.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('ResizeObserver', FakeResizeObserver);
    vi.spyOn(store, 'loadOlderMessages').mockResolvedValue();
    store.state.sync.messagesByChannel[7] = manyMessages(7, 5).map((m, i) =>
      i === 4 ? { ...m, outbox_id: 55 } : m,
    );
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete store.state.sync.messagesByChannel[7];
    store.state.focus = null;
  });

  function mountWithFocus(rowTop: number, focus: { messageId: number | null; outboxId: number | null }) {
    store.state.focus = { channelId: 7, ...focus, seq: 1 };
    const wrapper = mount(MessageFeed, { props: { channelId: 7 }, attachTo: document.body });
    const el = wrapper.get('.message-feed').element as HTMLElement;
    const geo = fakeGeometry(el, 2000);
    const row = el.querySelector('[data-message-id="5"]') as HTMLElement;
    // rowTop — абсолютное положение в ленте; в окне оно сдвинуто на текущий scrollTop
    row.getBoundingClientRect = () => ({ top: rowTop - el.scrollTop, bottom: rowTop + 40 - el.scrollTop }) as DOMRect;
    const scrollIntoView = vi.fn();
    row.scrollIntoView = scrollIntoView;
    return { wrapper, el, geo, scrollIntoView };
  }

  it('строка черновика в последнем экране: лента внизу и держится при росте', async () => {
    const { wrapper, el, geo, scrollIntoView } = mountWithFocus(1700, { messageId: null, outboxId: 55 });
    await vi.advanceTimersByTimeAsync(500);
    expect(scrollIntoView).not.toHaveBeenCalled();
    expect(el.scrollTop).toBe(2000);
    geo.grow(3200); // дорастание картинок выше
    FakeResizeObserver.fire();
    expect(el.scrollTop).toBe(3200);
    wrapper.unmount();
  });

  it('цель глубже: по центру, дорастание выше возвращает её в центр, прокрутка владельца отпускает', async () => {
    const { wrapper, el, geo, scrollIntoView } = mountWithFocus(300, { messageId: 5, outboxId: null });
    await vi.advanceTimersByTimeAsync(500);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    geo.grow(3200);
    FakeResizeObserver.fire();
    expect(scrollIntoView).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(500);
    el.scrollTop = 100; // владелец крутит сам
    el.dispatchEvent(new Event('scroll'));
    FakeResizeObserver.fire();
    expect(scrollIntoView).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });
});
