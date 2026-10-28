// Спека `docs/architecture.md`, группа 5: панель треда — загрузка страницы, `parent: null`,
// прокрутка вверх -> `next_before`, «прочитано» зовёт тредовую ручку (не канальную), отправка из панели идёт
// с `thread`, черновики ленты и треда независимы, Esc закрывает, смена канала закрывает.
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Message } from '../src/api/types';
import Composer from '../src/components/Composer.vue';
import ThreadPanel from '../src/components/ThreadPanel.vue';
import { store } from '../src/store/instance';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
    project_id: 1,
    ext_id: '1',
    thread_ext_id: 'P1',
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'ответ',
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

beforeEach(() => {
  store.state.openThread = null;
  store.state.sync.threads = {};
  store.state.sync.channelsById = {};
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('22 Панель треда: видимость и содержимое', () => {
  it('openThread=null -> панель вообще не рендерится', () => {
    const wrapper = mount(ThreadPanel);
    expect(wrapper.find('.column-thread').exists()).toBe(false);
  });

  it('parent=null -> заглушка «исходное сообщение не загружено»', () => {
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    store.state.sync.threads['1:P1'] = { parent: null, replies: [], nextBefore: null, loading: false };
    const wrapper = mount(ThreadPanel);
    expect(wrapper.find('.thread-parent-missing').text()).toBe('исходное сообщение не загружено');
    expect(wrapper.find('.thread-parent').exists()).toBe(false);
  });

  it('parent известен -> отрисован, ответы страницей — под ним, загрузка через loading', () => {
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    store.state.sync.threads['1:P1'] = {
      parent: msg({ id: 1, ext_id: 'P1', thread_ext_id: null, text: 'родитель' }),
      replies: [msg({ id: 2, ext_id: 'R1', text: 'первый ответ' })],
      nextBefore: null,
      loading: true,
    };
    const wrapper = mount(ThreadPanel);
    expect(wrapper.find('.thread-parent-missing').exists()).toBe(false);
    expect(wrapper.find('.thread-parent').text()).toContain('родитель');
    expect(wrapper.text()).toContain('первый ответ');
    expect(wrapper.find('.loading-hint').exists()).toBe(true);
  });

  it('Esc закрывает панель', async () => {
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    store.state.sync.threads['1:P1'] = { parent: null, replies: [], nextBefore: null, loading: false };
    mount(ThreadPanel, { attachTo: document.body });
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(store.state.openThread).toBeNull();
  });
});

describe('22 Панель треда: прокрутка вверх грузит следующую страницу (loadOlderReplies)', () => {
  it('scroll у верхнего края с непустым nextBefore зовёт store.loadOlderReplies', async () => {
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    store.state.sync.threads['1:P1'] = {
      parent: null,
      replies: [msg({ id: 2 })],
      nextBefore: '900,0,2',
      loading: false,
    };
    const spy = vi.spyOn(store, 'loadOlderReplies').mockResolvedValue();
    const wrapper = mount(ThreadPanel, { attachTo: document.body });
    await wrapper.find('.thread-messages').trigger('scroll');
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe('22 Панель треда: «прочитано» зовёт ТРЕДОВУЮ ручку, не канальную', () => {
  class RecordingIntersectionObserver {
    static instances: RecordingIntersectionObserver[] = [];
    callback: IntersectionObserverCallback;
    constructor(callback: IntersectionObserverCallback) {
      this.callback = callback;
      RecordingIntersectionObserver.instances.push(this);
    }
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
    takeRecords(): IntersectionObserverEntry[] {
      return [];
    }
    fire(target: Element, isIntersecting: boolean): void {
      this.callback(
        [{ target, isIntersecting } as IntersectionObserverEntry],
        this as unknown as IntersectionObserver,
      );
    }
  }

  beforeEach(() => {
    RecordingIntersectionObserver.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal('IntersectionObserver', RecordingIntersectionObserver);
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('нижнее видимое сообщение треда -> store.markThreadRead(channelId, threadExtId, id), не markRead канала', async () => {
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    store.state.sync.threads['1:P1'] = {
      parent: msg({ id: 1, ext_id: 'P1', thread_ext_id: null }),
      replies: [msg({ id: 20, ext_id: 'R1' })],
      nextBefore: null,
      loading: false,
    };
    const threadReadSpy = vi.spyOn(store, 'markThreadRead').mockResolvedValue();
    const channelReadSpy = vi.spyOn(store, 'markRead').mockResolvedValue();

    const wrapper = mount(ThreadPanel, { attachTo: document.body });
    await wrapper.vm.$nextTick();

    const target = wrapper.get('[data-message-id="20"]').element;
    const observer = RecordingIntersectionObserver.instances.at(-1)!;
    observer.fire(target, true);

    await vi.advanceTimersByTimeAsync(600);

    expect(threadReadSpy).toHaveBeenCalledWith(1, 'P1', 20);
    expect(channelReadSpy).not.toHaveBeenCalled();
  });
});

describe('22 Панель треда: отправка идёт с thread=, черновики ленты/треда независимы', () => {
  it('Composer, смонтированный панелью треда, отправляет с thread=threadExtId и БЕЗ replyTo', async () => {
    const sendSpy = vi.spyOn(store, 'send').mockResolvedValue();
    const wrapper = mount(Composer, { props: { channelId: 1, thread: 'P1' } });
    await wrapper.find('textarea').setValue('ответ в тред');
    await wrapper.find('form').trigger('submit');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sendSpy).toHaveBeenCalledWith(1, 'ответ в тред', expect.any(String), {
      thread: 'P1',
      replyTo: undefined,
    });
  });

  it('черновики двух независимых Composer (лента и панель треда) не пересекаются', async () => {
    vi.spyOn(store, 'send').mockResolvedValue();
    const feedComposer = mount(Composer, { props: { channelId: 1 } });
    const threadComposer = mount(Composer, { props: { channelId: 1, thread: 'P1' } });

    await feedComposer.find('textarea').setValue('текст в ленте');
    await threadComposer.find('textarea').setValue('текст в треде');

    expect((feedComposer.find('textarea').element as HTMLTextAreaElement).value).toBe('текст в ленте');
    expect((threadComposer.find('textarea').element as HTMLTextAreaElement).value).toBe('текст в треде');
  });
});

describe('22 Панель треда: смена канала закрывает панель', () => {
  it('store.openChannel() сбрасывает openThread в null', async () => {
    vi.spyOn(api, 'listChannels').mockResolvedValue([]);
    vi.spyOn(api, 'listMessages').mockResolvedValue({ messages: [], next_before: null });
    store.state.openThread = { channelId: 1, threadExtId: 'P1' };
    await store.openChannel(2);
    expect(store.state.openThread).toBeNull();
  });
});
