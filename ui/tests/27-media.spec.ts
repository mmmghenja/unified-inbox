// Ф8-5 B (docs/architecture.md): медиа в ленте. T8 — готовое фото -> <img> с `/api/v1/media/{id}`,
// клик -> просмотр, альбом -> сетка; T9 — «Загрузить» шлёт `fetch`, заглушка «загружается…», надписи
// failed/gone/too_big, документ -> «Скачать»; П15 — показ по `detected`, не по `kind`; П14 — без кнопки у
// источников без медиа; контракт 5b — надписи `ttl`/`external`.
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, MediaItem, Message } from '../src/api/types';
import AlbumRow from '../src/components/AlbumRow.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { parseAttachments } from '../src/render/attachments';
import { buildAttachmentViews, displayFor, previewSize } from '../src/render/media';
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

const MB = 1024 * 1024;

beforeEach(() => {
  setSource('telegram');
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete store.state.sync.channelsById[1];
  delete store.state.sync.messagesByChannel[1];
});

describe('27 media: готовое фото (T8)', () => {
  it('<img> c /api/v1/media/{id}, lazy, вписано в 320×320 по пропорциям; клик — просмотр, Esc — закрыть', async () => {
    const wrapper = mount(MessageRow, {
      attachTo: document.body,
      props: {
        message: msg({
          attachments_json: att([{ type: 'photo', name: 'кот.jpg' }]),
          media: [item({ id: 5, width: 800, height: 600 })],
        }),
      },
    });
    const img = wrapper.get('img.media-img');
    expect(img.attributes('src')).toBe('/api/v1/media/5');
    expect(img.attributes('loading')).toBe('lazy');
    const thumb = wrapper.get('button.media-thumb');
    expect((thumb.element as HTMLElement).style.width).toBe('320px');
    expect((thumb.element as HTMLElement).style.height).toBe('240px');
    expect(wrapper.find('.media-viewer').exists()).toBe(false);

    await thumb.trigger('click');
    const viewer = wrapper.get('.media-viewer');
    expect(viewer.get('img').attributes('src')).toBe('/api/v1/media/5');

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.media-viewer').exists()).toBe(false);

    await thumb.trigger('click');
    await wrapper.get('.media-viewer').trigger('click');
    expect(wrapper.find('.media-viewer').exists()).toBe(false);
    wrapper.unmount();
  });

  it('альбом — сетка превью, в просмотре листание стрелками', async () => {
    const messages = [1, 2, 3].map((id) =>
      msg({
        id,
        ext_id: String(id),
        ts: 1000 + id,
        grouped_id: 'g1',
        attachments_json: att([{ type: 'photo' }]),
        media: [item({ id: 10 + id })],
      }),
    );
    const wrapper = mount(AlbumRow, { attachTo: document.body, props: { messages } });
    const grid = wrapper.get('.media-grid');
    expect(grid.classes()).toContain('media-grid--album');
    const imgs = grid.findAll('img');
    expect(imgs.map((i) => i.attributes('src'))).toEqual([
      '/api/v1/media/11',
      '/api/v1/media/12',
      '/api/v1/media/13',
    ]);
    await wrapper.findAll('button.media-thumb')[0]!.trigger('click');
    expect(wrapper.get('.media-viewer__counter').text()).toBe('1 / 3');
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    await wrapper.vm.$nextTick();
    expect(wrapper.get('.media-viewer__counter').text()).toBe('2 / 3');
    expect(wrapper.get('.media-viewer img').attributes('src')).toBe('/api/v1/media/12');
    await wrapper.get('.media-viewer__next').trigger('click');
    expect(wrapper.get('.media-viewer__counter').text()).toBe('3 / 3');
    expect(wrapper.find('.media-viewer').exists()).toBe(true); // кнопки листания не закрывают просмотр
    wrapper.unmount();
  });
});

describe('27 media: статусы и кнопки (T9)', () => {
  it('deferred — «Загрузить (<размер>)» шлёт fetch с id сообщения и idx вложения', async () => {
    const spy = vi.spyOn(store, 'fetchMedia').mockResolvedValue();
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          id: 42,
          attachments_json: att([{ type: 'location' }, { type: 'video', size: 25 * MB }]),
          media: [item({ idx: 1, kind: 'video', status: 'deferred', size: 25 * MB, detected: null })],
        }),
      },
    });
    const button = wrapper.get('button.media-fetch');
    expect(button.text()).toBe('Загрузить (25 МБ)');
    await button.trigger('click');
    expect(spy).toHaveBeenCalledWith(42, 1);
  });

  it('старое вложение без строки media — «Загрузить»; у источника без медиа кнопки нет (П14)', async () => {
    const spy = vi.spyOn(store, 'fetchMedia').mockResolvedValue();
    const message = msg({ id: 3, attachments_json: att([{ type: 'photo', size: 2048 }]), media: [] });
    const wrapper = mount(MessageRow, { props: { message } });
    await wrapper.get('button.media-fetch').trigger('click');
    expect(spy).toHaveBeenCalledWith(3, 0);

    setSource('fake');
    const other = mount(MessageRow, { props: { message } });
    expect(other.find('button.media-fetch').exists()).toBe(false);
    expect(other.text()).toContain('📎 Фото 2 КБ');
  });

  it('pending/fetching — заглушка «загружается…» по пропорциям; без пропорций — строка', () => {
    const sized = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'photo' }]),
          media: [item({ status: 'pending', width: 100, height: 400, detected: null })],
        }),
      },
    });
    const ph = sized.get('.media-placeholder');
    expect(ph.text()).toBe('загружается…');
    expect((ph.element as HTMLElement).style.height).toBe('320px');
    expect((ph.element as HTMLElement).style.width).toBe('80px');

    const plain = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'document', name: 'a.zip' }]),
          media: [item({ kind: 'document', status: 'fetching', detected: null })],
        }),
      },
    });
    expect(plain.text()).toContain('загружается…');
    expect(plain.find('img').exists()).toBe(false);
  });

  it('failed — «не удалось загрузить · Повторить»; gone — «файла больше нет в Slack»; too_big — с размером', async () => {
    const spy = vi.spyOn(store, 'fetchMedia').mockResolvedValue();
    const failed = mount(MessageRow, {
      props: {
        message: msg({
          id: 9,
          attachments_json: att([{ type: 'photo' }]),
          media: [item({ status: 'failed', detected: null })],
        }),
      },
    });
    expect(failed.text()).toContain('не удалось загрузить');
    const retry = failed.get('button.media-fetch');
    expect(retry.text()).toBe('Повторить');
    await retry.trigger('click');
    expect(spy).toHaveBeenCalledWith(9, 0);

    setSource('slack');
    const gone = mount(MessageRow, {
      props: {
        message: msg({ attachments_json: att([{ type: 'photo' }]), media: [item({ status: 'gone', detected: null })] }),
      },
    });
    expect(gone.text()).toContain('файла больше нет в Slack');
    expect(gone.find('button').exists()).toBe(true); // только «Ответить» строки сообщения
    expect(gone.find('button.media-fetch').exists()).toBe(false);

    const big = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'video' }]),
          media: [item({ kind: 'video', status: 'too_big', size: 400 * MB, detected: null })],
        }),
      },
    });
    expect(big.text()).toContain('слишком большой (400 МБ)');
    expect(big.find('button.media-fetch').exists()).toBe(false);
  });

  it('документ — «<имя> · <размер> · Скачать» ссылкой на /api/v1/media/{id}', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'document', name: 'план.pdf', size: 2048 }]),
          media: [item({ id: 77, kind: 'document', size: 2048, detected: 'pdf' })],
        }),
      },
    });
    expect(wrapper.text()).toContain('📎 план.pdf · 2 КБ ·');
    const link = wrapper.get('a.media-download');
    expect(link.text()).toBe('Скачать');
    expect(link.attributes('href')).toBe('/api/v1/media/77');
    expect(link.attributes('download')).toBeDefined();
    expect(link.attributes('target')).toBeUndefined();
  });

  it('видео — <video controls>, голосовое — <audio controls>, GIF (mp4) — без звука по кругу', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([{ type: 'video' }, { type: 'voice' }, { type: 'gif' }]),
          media: [
            item({ id: 1, idx: 0, kind: 'video', detected: 'mp4' }),
            item({ id: 2, idx: 1, kind: 'voice', detected: 'ogg' }),
            item({ id: 3, idx: 2, kind: 'gif', detected: 'mp4' }),
          ],
        }),
      },
    });
    const videos = wrapper.findAll('video');
    expect(videos).toHaveLength(2);
    expect(videos[0]!.attributes('src')).toBe('/api/v1/media/1');
    expect(videos[0]!.attributes('controls')).toBeDefined();
    expect(videos[0]!.attributes('preload')).toBe('metadata');
    const loop = videos[1]!.element as HTMLVideoElement;
    expect(videos[1]!.attributes('src')).toBe('/api/v1/media/3');
    expect(loop.muted).toBe(true);
    expect(loop.loop).toBe(true);
    expect(loop.autoplay).toBe(true);
    expect(videos[1]!.attributes('controls')).toBeUndefined();
    const audio = wrapper.get('audio');
    expect(audio.attributes('src')).toBe('/api/v1/media/2');
    expect(audio.attributes('controls')).toBeDefined();
  });

  it('одноразовое Telegram и внешний файл Slack — надписи, без кнопки', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: att([
            { type: 'photo', ttl: true },
            { type: 'document', external: true, name: 'doc' },
          ]),
          media: [],
        }),
      },
    });
    expect(wrapper.text()).toContain('одноразовое — смотри в Telegram');
    expect(wrapper.text()).toContain('внешний файл — открыть в Slack');
    expect(wrapper.find('button.media-fetch').exists()).toBe(false);
    expect(wrapper.find('a').exists()).toBe(false);
  });

  it('удалённое отправителем — медиа показывается так же, строка приглушена (B6)', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ deleted: true, attachments_json: att([{ type: 'photo' }]), media: [item({ id: 8 })] }),
      },
    });
    expect(wrapper.classes()).toContain('message-row--deleted');
    expect(wrapper.get('img').attributes('src')).toBe('/api/v1/media/8');
  });
});

describe('27 media: выбор показа — чистая функция', () => {
  it('по detected, не по kind и не по mime (П15, П7)', () => {
    const [photo] = parseAttachments(att([{ type: 'photo', mime: 'image/png' }]));
    expect(displayFor(photo!, item({ detected: 'other' }), 'telegram')).toBe('file');
    expect(displayFor(photo!, item({ detected: 'png' }), 'telegram')).toBe('image');
    const [sticker] = parseAttachments(att([{ type: 'sticker' }]));
    expect(displayFor(sticker!, item({ detected: 'webm' }), 'telegram')).toBe('loop');
    expect(displayFor(sticker!, item({ detected: 'webp' }), 'telegram')).toBe('image');
    const [doc] = parseAttachments(att([{ type: 'document' }]));
    expect(displayFor(doc!, item({ detected: 'mp4' }), 'slack')).toBe('video');
  });

  it('gone-флаг без строки — «файла больше нет»; геопозиция — просто подпись', () => {
    const views = buildAttachmentViews(
      1,
      parseAttachments(att([{ type: 'photo', gone: true }, { type: 'location' }])),
      [],
      'slack',
    );
    expect(views.map((v) => v.display)).toEqual(['gone', 'none']);
  });

  it('previewSize вписывает в 320×320 и не увеличивает маленькое', () => {
    expect(previewSize(1000, 500)).toEqual({ width: 320, height: 160 });
    expect(previewSize(100, 50)).toEqual({ width: 100, height: 50 });
    expect(previewSize(null, 50)).toBeNull();
  });
});

describe('27 media: store.fetchMedia (П8)', () => {
  it('POST …/media/{idx}/fetch, обновлённый media сразу в сообщении стора', async () => {
    const updated = [item({ id: 31, idx: 0, status: 'pending', detected: null })];
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/fetch')) {
        return Promise.resolve(
          new Response(JSON.stringify({ message_id: 4, media: updated }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ messages: [], channels: [], max_seq: 0, more: false }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      );
    });
    vi.stubGlobal('fetch', fetchMock);
    api.setCsrf('c');
    store.state.sync.messagesByChannel[1] = [msg({ id: 4, attachments_json: att([{ type: 'photo' }]) })];

    await store.fetchMedia(4, 0);

    const [url, init] = fetchMock.mock.calls[0]! as [string, { method: string }];
    expect(url).toBe('/api/v1/messages/4/media/0/fetch');
    expect(init.method).toBe('POST');
    expect(store.state.sync.messagesByChannel[1]![0]!.media).toEqual(updated);
  });
});
