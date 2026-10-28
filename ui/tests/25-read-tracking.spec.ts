// Ф8-1 B (`docs/architecture.md`), тесты T6–T7: прочитанность длинных сообщений и возврат фокуса.
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Message } from '../src/api/types';
import MessageFeed from '../src/components/MessageFeed.vue';
import ThreadPanel from '../src/components/ThreadPanel.vue';
import { store } from '../src/store/instance';
import { isMessageShown, READ_OBSERVER_THRESHOLDS } from '../src/store/readTracker';

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

describe('T6: isMessageShown — чистая функция без DOM', () => {
  const ROOT = 600;

  it('короткое сообщение видно на 60 % — показано', () => {
    expect(isMessageShown({ intersectionRatio: 0.6, visibleHeight: 60, rootHeight: ROOT })).toBe(true);
  });

  it('короткое сообщение видно на 50 % — не показано', () => {
    expect(isMessageShown({ intersectionRatio: 0.5, visibleHeight: 50, rootHeight: ROOT })).toBe(false);
  });

  it('длинное (выше ленты) при доле 0.3, занимает ≥ 60 % ленты — показано', () => {
    // письмо 1500 px, видно 0.3 * 1500 = 450 px = 75 % ленты
    expect(isMessageShown({ intersectionRatio: 0.3, visibleHeight: 450, rootHeight: ROOT })).toBe(true);
    // ровно 60 % ленты
    expect(isMessageShown({ intersectionRatio: 0.24, visibleHeight: 360, rootHeight: ROOT })).toBe(true);
  });

  it('длинное, занимающее 20 % ленты, — не показано', () => {
    expect(isMessageShown({ intersectionRatio: 0.08, visibleHeight: 120, rootHeight: ROOT })).toBe(false);
  });

  it('высота ленты неизвестна (0) — решает только доля самого сообщения', () => {
    expect(isMessageShown({ intersectionRatio: 0.3, visibleHeight: 450, rootHeight: 0 })).toBe(false);
  });

  it('наблюдатель — с несколькими порогами, среди них 0 и 1 (не единственный 0.6)', () => {
    expect(READ_OBSERVER_THRESHOLDS.length).toBeGreaterThan(4);
    expect(READ_OBSERVER_THRESHOLDS).toContain(0);
    expect(READ_OBSERVER_THRESHOLDS).toContain(0.6);
    expect(READ_OBSERVER_THRESHOLDS).toContain(1);
  });
});

// Фейковый наблюдатель, как в `22-thread-panel.spec.ts`, — но запись несёт долю и высоты (B1).
class RecordingIntersectionObserver {
  static instances: RecordingIntersectionObserver[] = [];
  callback: IntersectionObserverCallback;
  options: IntersectionObserverInit | undefined;
  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.callback = callback;
    this.options = options;
    RecordingIntersectionObserver.instances.push(this);
  }
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
  fire(target: Element, ratio: number, visibleHeight: number, rootHeight: number): void {
    this.callback(
      [
        {
          target,
          isIntersecting: ratio > 0,
          intersectionRatio: ratio,
          intersectionRect: { height: visibleHeight } as DOMRectReadOnly,
          rootBounds: { height: rootHeight } as DOMRectReadOnly,
        } as IntersectionObserverEntry,
      ],
      this as unknown as IntersectionObserver,
    );
  }
}

/** Лента «прокручена вверх» (не у низа) — иначе в jsdom (все размеры 0) она всегда «у низа» (B2). */
function scrollAwayFromBottom(el: Element): void {
  Object.defineProperty(el, 'scrollHeight', { value: 3000, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: 600, configurable: true });
  (el as HTMLElement).scrollTop = 0;
}

describe('T7 и B1–B2: лента канала — длинное письмо, возврат фокуса, низ ленты', () => {
  let hasFocus: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    RecordingIntersectionObserver.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('IntersectionObserver', RecordingIntersectionObserver);
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    hasFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    vi.spyOn(store, 'loadOlderMessages').mockResolvedValue();
    store.state.sync.messagesByChannel[7] = [
      msg({ id: 71, ext_id: '71', ts: 1000 }),
      msg({ id: 72, ext_id: '72', ts: 1001 }),
      msg({ id: 73, ext_id: '73', ts: 1002 }),
    ];
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    delete store.state.sync.messagesByChannel[7];
  });

  async function mountFeed() {
    const wrapper = mount(MessageFeed, { props: { channelId: 7 }, attachTo: document.body });
    scrollAwayFromBottom(wrapper.get('.message-feed').element);
    await vi.advanceTimersByTimeAsync(0);
    // автопрокрутка при монтировании уводит ленту вниз — возвращаем вверх (пользователь читает историю)
    (wrapper.get('.message-feed').element as HTMLElement).scrollTop = 0;
    const observer = RecordingIntersectionObserver.instances.at(-1)!;
    return { wrapper, observer };
  }

  it('наблюдатель создаётся с несколькими порогами', async () => {
    const { observer, wrapper } = await mountFeed();
    expect(Array.isArray(observer.options?.threshold)).toBe(true);
    expect((observer.options!.threshold as number[]).length).toBeGreaterThan(4);
    wrapper.unmount();
  });

  it('длинное письмо (доля 0.3, но 75 % ленты) — «прочитано» до него уходит', async () => {
    const markRead = vi.spyOn(store, 'markRead').mockResolvedValue();
    const { wrapper, observer } = await mountFeed();
    observer.fire(wrapper.get('[data-message-id="72"]').element, 0.3, 450, 600);
    await vi.advanceTimersByTimeAsync(600);
    expect(markRead).toHaveBeenCalledWith(7, 72);
    wrapper.unmount();
  });

  it('T7: окно без фокуса во время таймера — markRead не ушёл; focus -> ушёл до нижнего показанного', async () => {
    const markRead = vi.spyOn(store, 'markRead').mockResolvedValue();
    hasFocus.mockReturnValue(false);
    const { wrapper, observer } = await mountFeed();
    observer.fire(wrapper.get('[data-message-id="71"]').element, 1, 40, 600);
    observer.fire(wrapper.get('[data-message-id="72"]').element, 0.8, 32, 600);
    await vi.advanceTimersByTimeAsync(600);
    expect(markRead).not.toHaveBeenCalled();

    hasFocus.mockReturnValue(true);
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(600);
    expect(markRead).toHaveBeenCalledTimes(1);
    expect(markRead).toHaveBeenCalledWith(7, 72);
    wrapper.unmount();
  });

  it('B3: возврат вкладки (visibilitychange -> visible) тоже перезапускает отправку', async () => {
    const markRead = vi.spyOn(store, 'markRead').mockResolvedValue();
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    const { wrapper, observer } = await mountFeed();
    observer.fire(wrapper.get('[data-message-id="71"]').element, 1, 40, 600);
    await vi.advanceTimersByTimeAsync(600);
    expect(markRead).not.toHaveBeenCalled();

    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(600);
    expect(markRead).toHaveBeenCalledWith(7, 71);
    wrapper.unmount();
  });

  it('после размонтирования focus ничего не шлёт (слушатели сняты)', async () => {
    const markRead = vi.spyOn(store, 'markRead').mockResolvedValue();
    hasFocus.mockReturnValue(false);
    const { wrapper, observer } = await mountFeed();
    observer.fire(wrapper.get('[data-message-id="71"]').element, 1, 40, 600);
    await vi.advanceTimersByTimeAsync(600);
    wrapper.unmount();

    hasFocus.mockReturnValue(true);
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(600);
    expect(markRead).not.toHaveBeenCalled();
  });

  it('B2: лента у низа — последнее сообщение считается показанным без записи наблюдателя', async () => {
    const markRead = vi.spyOn(store, 'markRead').mockResolvedValue();
    const { wrapper, observer } = await mountFeed();
    const feed = wrapper.get('.message-feed').element as HTMLElement;
    feed.scrollTop = 2400; // 3000 - 2400 - 600 = 0 -> у низа
    // запись только о верхнем сообщении, последнее (73) наблюдатель не отдал
    observer.fire(wrapper.get('[data-message-id="71"]').element, 1, 40, 600);
    await vi.advanceTimersByTimeAsync(600);
    expect(markRead).toHaveBeenCalledWith(7, 73);
    wrapper.unmount();
  });
});

describe('B4: панель треда — та же логика (длинный ответ, возврат фокуса)', () => {
  let hasFocus: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    RecordingIntersectionObserver.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('IntersectionObserver', RecordingIntersectionObserver);
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    hasFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    vi.spyOn(store, 'loadOlderReplies').mockResolvedValue();
    store.state.openThread = { channelId: 7, threadExtId: 'P1' };
    store.state.sync.threads = {
      '7:P1': {
        parent: msg({ id: 70, ext_id: 'P1' }),
        replies: [
          msg({ id: 81, ext_id: 'R1', thread_ext_id: 'P1', ts: 1001 }),
          msg({ id: 82, ext_id: 'R2', thread_ext_id: 'P1', ts: 1002 }),
        ],
        nextBefore: null,
        loading: false,
      },
    };
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    store.state.openThread = null;
    store.state.sync.threads = {};
  });

  it('длинный ответ (доля 0.2, 70 % панели) после возврата фокуса -> markThreadRead', async () => {
    const markThreadRead = vi.spyOn(store, 'markThreadRead').mockResolvedValue();
    hasFocus.mockReturnValue(false);
    const wrapper = mount(ThreadPanel, { attachTo: document.body });
    scrollAwayFromBottom(wrapper.get('.thread-messages').element);
    await vi.advanceTimersByTimeAsync(0);
    (wrapper.get('.thread-messages').element as HTMLElement).scrollTop = 0;
    const observer = RecordingIntersectionObserver.instances.at(-1)!;
    observer.fire(wrapper.get('[data-message-id="81"]').element, 0.2, 420, 600);
    await vi.advanceTimersByTimeAsync(600);
    expect(markThreadRead).not.toHaveBeenCalled();

    hasFocus.mockReturnValue(true);
    window.dispatchEvent(new Event('focus'));
    await vi.advanceTimersByTimeAsync(600);
    expect(markThreadRead).toHaveBeenCalledWith(7, 'P1', 81);
    wrapper.unmount();
  });
});
