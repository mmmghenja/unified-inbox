// Ф11-3a A4 (docs/architecture.md), тест 5: вложения писем — как у мессенджеров. Нет строки
// `media` — «Загрузить (<размер>)», клик шлёт `fetch`; готовая картинка письма — превью; готовый PDF — файл по
// ссылке скачивания; `gone` — «файла больше нет в почте».
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Channel, MediaItem, Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { displayFor, sourceLabel } from '../src/render/media';
import { kindFromMime, parseAttachments } from '../src/render/attachments';
import { store } from '../src/store/instance';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
    project_id: 1,
    ext_id: '1',
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'file',
    text: null,
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
    sender: { id: 7, name: 'Аня', handle: null },
    media: [],
    ...overrides,
  };
}

function item(overrides: Partial<MediaItem>): MediaItem {
  return {
    id: 5,
    idx: 0,
    kind: 'photo',
    status: 'ready',
    mime: null,
    size: null,
    name: null,
    width: null,
    height: null,
    duration_s: null,
    detected: 'jpeg',
    ...overrides,
  };
}

function setSource(source: string): void {
  store.state.sync.channelsById[1] = { id: 1, source } as unknown as Channel;
}

function att(list: unknown[]): string {
  return JSON.stringify(list);
}

const KB = 1024;

beforeEach(() => {
  setSource('mail');
});

afterEach(() => {
  vi.restoreAllMocks();
  delete store.state.sync.channelsById[1];
});

describe('42 mail media: вложения писем по клику', () => {
  it('вложение письма без строки media — «Загрузить (<размер>)», клик шлёт fetch', async () => {
    const spy = vi.spyOn(store, 'fetchMedia').mockResolvedValue();
    const message = msg({
      id: 9,
      kind: 'text',
      text: 'Счёт во вложении.',
      attachments_json: att([
        { type: 'photo', name: 'logo.png', mime: 'image/png', size: 2 * KB },
        { type: 'document', name: 'счет.pdf', mime: 'application/pdf', size: 30 * KB },
      ]),
      media: [],
    });
    const wrapper = mount(MessageRow, { props: { message } });
    const buttons = wrapper.findAll('button.media-fetch');
    expect(buttons.map((b) => b.text())).toEqual(['Загрузить (2 КБ)', 'Загрузить (30 КБ)']);
    await buttons[1]!.trigger('click');
    expect(spy).toHaveBeenCalledWith(9, 1);
  });

  it('deferred-строка письма — тоже «Загрузить»', () => {
    const [a] = parseAttachments(att([{ type: 'document', name: 'a.pdf', size: 10 }]));
    expect(displayFor(a!, item({ kind: 'document', status: 'deferred', detected: null }), 'mail')).toBe('fetch');
    expect(displayFor(a!, null, 'mail')).toBe('fetch');
  });

  it('ready-картинка письма — превью <img> с /api/v1/media/{id}', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'photo', name: 'фото.jpg', mime: 'image/jpeg', size: 4 * KB }]),
          media: [item({ id: 77, detected: 'jpeg' })],
        }),
      },
    });
    expect(wrapper.get('img.media-img').attributes('src')).toBe('/api/v1/media/77');
    expect(wrapper.find('button.media-fetch').exists()).toBe(false);
  });

  it('ready-PDF письма — файл по ссылке скачивания', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'document', name: 'счет.pdf', mime: 'application/pdf', size: 30 * KB }]),
          media: [item({ id: 78, kind: 'document', detected: 'pdf', size: 30 * KB })],
        }),
      },
    });
    const link = wrapper.get('a.media-download');
    expect(link.attributes('href')).toContain('/api/v1/media/78');
    expect(wrapper.find('img.media-img').exists()).toBe(false);
  });

  it('gone — «файла больше нет в почте»', () => {
    expect(sourceLabel('mail')).toBe('почте');
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'document', name: 'a.pdf', size: 10 }]),
          media: [item({ kind: 'document', status: 'gone', detected: null })],
        }),
      },
    });
    expect(wrapper.text()).toContain('файла больше нет в почте');
  });

  it('старое письмо без type — вид по mime, в ленте имя, размер и «Загрузить»; клик зовёт fetch', async () => {
    const spy = vi.spyOn(store, 'fetchMedia').mockResolvedValue();
    const message = msg({
      id: 11,
      kind: 'text',
      text: 'Старое письмо',
      attachments_json: att([
        { name: 'фото.jpg', mime: 'image/jpeg', size: 3 * KB },
        { name: 'договор.docx', mime: 'application/vnd.openxmlformats', size: 5 * KB },
        { name: 'без mime' },
      ]),
      media: [],
    });
    const parsed = parseAttachments(message.attachments_json);
    expect(parsed.map((a) => [a.idx, a.type])).toEqual([
      [0, 'photo'],
      [1, 'document'],
    ]);
    const wrapper = mount(MessageRow, { props: { message } });
    expect(wrapper.text()).toContain('фото.jpg');
    expect(wrapper.text()).toContain('договор.docx');
    expect(wrapper.text()).not.toContain('без mime');
    const buttons = wrapper.findAll('button.media-fetch');
    expect(buttons.map((b) => b.text())).toEqual(['Загрузить (3 КБ)', 'Загрузить (5 КБ)']);
    await buttons[1]!.trigger('click');
    expect(spy).toHaveBeenCalledWith(11, 1);
  });

  it('kindFromMime — правило ядра', () => {
    expect(kindFromMime('image/PNG')).toBe('photo');
    expect(kindFromMime('image/gif; name=a.gif')).toBe('photo');
    expect(kindFromMime('image/svg+xml')).toBe('document');
    expect(kindFromMime('application/pdf')).toBe('document');
  });

  it('неизвестный type с mime по-прежнему отбрасывается', () => {
    expect(parseAttachments(att([{ type: 'bogus', mime: 'image/png' }]))).toEqual([]);
  });
});
