// Ф27-1b: подсказка `@` в поле ввода беседы Slack (только Slack), выбор вставляет `@Имя`, отправка уходит с
// `<@id>`; не ушло — метки возвращаются в поле; черновик с `<@id>` показан и правится как `@Имя`.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, MentionPerson, OutboxListItem } from '../src/api/types';
import Composer from '../src/components/Composer.vue';
import DraftCard from '../src/components/DraftCard.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import { store } from '../src/store/instance';

const ANNA: MentionPerson = { id: 'U111', name: 'Анна Лим', handle: '@anna' };
const PETR: MentionPerson = { id: 'U222', name: 'Пётр', handle: null };

function channel(source: string): Channel {
  return {
    id: 1, ext_id: 'c1', kind: 'group', title: 'Канал', tier: null, muted: false, archived: false,
    map_reason: null, last_msg_ts: 1000, my_msg_count: 0, first_seen_ts: null, unread: 0, project_id: 1, source,
  };
}

let wrapper: VueWrapper | null = null;
let peopleSpy: MockInstance;
let sendSpy: MockInstance;

async function mountComposer(source = 'slack'): Promise<VueWrapper> {
  store.state.sync.channelsById = { 1: channel(source) };
  wrapper = mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
  return wrapper;
}

async function type(w: VueWrapper, value: string): Promise<HTMLTextAreaElement> {
  const area = w.get('textarea').element as HTMLTextAreaElement;
  area.value = value;
  area.setSelectionRange(value.length, value.length);
  await w.get('textarea').trigger('input');
  return area;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(250);
  await flushPromises();
}

beforeEach(() => {
  vi.useFakeTimers();
  api.setCsrf('c');
  vi.spyOn(api, 'getChannelAccountId').mockResolvedValue(2);
  peopleSpy = vi.spyOn(api, 'searchMentionPeople').mockResolvedValue([ANNA, PETR]);
  sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined as never);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('подсказка @ в Composer', () => {
  it('Slack: @ан -> запрос с задержкой, список, выбор вставляет @Имя, отправка уходит с <@id>', async () => {
    const w = await mountComposer();
    await type(w, 'привет @ан');
    expect(peopleSpy).not.toHaveBeenCalled(); // задержка 200 мс
    await settle();
    expect(peopleSpy).toHaveBeenCalledWith(2, 'ан', 1);
    const items = w.findAll('.mention-suggest-item');
    expect(items.map((i) => i.find('.mention-suggest-name').text())).toEqual(['Анна Лим', 'Пётр']);
    await items[0]!.trigger('click');
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('привет @Анна Лим ');
    expect(w.find('.mention-suggest').exists()).toBe(false);
    await type(w, 'привет @Анна Лим глянь');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(sendSpy.mock.calls[0]![1]).toBe('привет <@U111> глянь');
  });

  it('стрелки и Enter выбирают человека, Esc закрывает без вставки', async () => {
    const w = await mountComposer();
    await type(w, '@п');
    await settle();
    await w.get('textarea').trigger('keydown', { key: 'ArrowDown' });
    expect(w.findAll('.mention-suggest-item')[1]!.classes()).toContain('mention-suggest-item--active');
    await w.get('textarea').trigger('keydown', { key: 'Enter' });
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('@Пётр ');
    await type(w, '@Пётр @а');
    await settle();
    expect(w.find('.mention-suggest').exists()).toBe(true);
    await w.get('textarea').trigger('keydown', { key: 'Escape' });
    expect(w.find('.mention-suggest').exists()).toBe(false);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('@Пётр @а');
  });

  it('удалённая метка уходит текстом, оставшаяся — упоминанием', async () => {
    const w = await mountComposer();
    await type(w, '@а');
    await settle();
    await w.findAll('.mention-suggest-item')[0]!.trigger('click');
    await type(w, 'Анна Лим без значка');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(sendSpy.mock.calls[0]![1]).toBe('Анна Лим без значка');
  });

  it('не ушло — в поле возвращается текст с метками, повторная отправка снова с <@id>', async () => {
    const w = await mountComposer();
    await type(w, '@а');
    await settle();
    await w.findAll('.mention-suggest-item')[0]!.trigger('click');
    sendSpy.mockRejectedValueOnce(new Error('сеть'));
    await w.get('form').trigger('submit');
    await flushPromises();
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('@Анна Лим');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(sendSpy.mock.calls[1]![1]).toBe('<@U111>');
  });

  it('Telegram и почта: подсказки нет, запроса нет, текст как набран', async () => {
    for (const source of ['telegram', 'mail']) {
      const w = await mountComposer(source);
      await type(w, '@ан');
      await settle();
      expect(peopleSpy).not.toHaveBeenCalled();
      expect(w.find('.mention-suggest').exists()).toBe(false);
      await w.get('form').trigger('submit');
      await flushPromises();
      expect(sendSpy.mock.calls.at(-1)![1]).toBe('@ан');
      w.unmount();
    }
  });

  it('одинаковые имена — метки с ником, оба уходят верно', async () => {
    peopleSpy.mockResolvedValue([ANNA, { id: 'U333', name: 'Анна Лим', handle: '@anna2' }]);
    const w = await mountComposer();
    await type(w, '@а');
    await settle();
    await w.findAll('.mention-suggest-item')[1]!.trigger('click');
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('@Анна Лим·anna2 ');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(sendSpy.mock.calls[0]![1]).toBe('<@U333>');
  });
});

describe('черновик со ссылкой <@id>', () => {
  function draft(): OutboxListItem {
    return {
      id: 5, channel_id: 1, project_id: 1, channel_title: 'Канал', text: 'привет <@U111>, глянь',
      status: 'pending_approval', origin: 'api', error: null, attempts: 0, created_ts: 0, sent_ts: null,
      ext_id: null, source: 'slack', kind: 'group', author: 'mcp:all', reply_to: null, thread: null,
      paced: false, not_before_ts: null, account_id: 2, files: [], mention_names: { U111: 'Анна Лим' },
    };
  }

  it('показан @Имя; правка — в поле @Имя, сохранение — обратно <@id>', async () => {
    const approve = vi.spyOn(store, 'approveOutbox').mockResolvedValue(undefined as never);
    const w = mount(DraftCard, { props: { item: draft() } });
    expect(w.get('.draft-card-text').text()).toBe('привет @Анна Лим, глянь');
    await w.get('.draft-edit').trigger('click');
    const field = w.get('textarea.draft-card-edit');
    expect((field.element as HTMLTextAreaElement).value).toBe('привет @Анна Лим, глянь');
    await field.setValue('привет @Анна Лим, глянь срочно');
    await w.get('.draft-send').trigger('click');
    await flushPromises();
    expect(approve).toHaveBeenCalledWith(5, 'привет <@U111>, глянь срочно');
  });

  it('без правки уходит тем же текстом (approve без текста)', async () => {
    const approve = vi.spyOn(store, 'approveOutbox').mockResolvedValue(undefined as never);
    const w = mount(DraftCard, { props: { item: draft() } });
    await w.get('.draft-send').trigger('click');
    await flushPromises();
    expect(approve).toHaveBeenCalledWith(5, undefined);
  });
});

describe('черновик в ленте (OutboxStatusBadge)', () => {
  it('правка в поле — @Имя, подтверждение — <@id>', async () => {
    const approve = vi.spyOn(store, 'approveOutbox').mockResolvedValue(undefined as never);
    const w = mount(OutboxStatusBadge, {
      props: {
        outboxId: 7,
        status: 'pending_approval',
        text: 'привет <@U111>, глянь',
        paced: false,
        notBeforeTs: null,
        mentionNames: { U111: 'Анна Лим' },
      },
    });
    const field = w.get('textarea');
    expect((field.element as HTMLTextAreaElement).value).toBe('привет @Анна Лим, глянь');
    await field.setValue('привет @Анна Лим, глянь срочно');
    await w.get('button').trigger('click');
    await flushPromises();
    expect(approve).toHaveBeenCalledWith(7, 'привет <@U111>, глянь срочно');
  });
});
