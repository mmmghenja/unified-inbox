// Стикеры Telegram в ленте: .webp — <img>, .webm — <video autoplay loop muted playsinline>, .tgs — Lottie
// (gzip -> `DecompressionStream`, библиотека грузится лениво); ошибка — текст «стикер 😀».
import { gzipSync } from 'node:zlib';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Channel, MediaItem, Message } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { parseAttachments } from '../src/render/attachments';
import { displayFor } from '../src/render/media';
import { store } from '../src/store/instance';

const loadAnimation = vi.fn(() => ({ destroy: vi.fn() }));
vi.mock('lottie-web/build/player/lottie_light', () => ({ default: { loadAnimation } }));

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
    kind: 'sticker',
    status: 'ready',
    mime: null,
    size: null,
    name: null,
    width: null,
    height: null,
    duration_s: null,
    detected: 'webp',
    ...overrides,
  };
}

const sticker = (format: string) => JSON.stringify([{ type: 'sticker', emoji: '😀', sticker_format: format }]);

beforeEach(() => {
  store.state.sync.channelsById[1] = { id: 1, source: 'telegram' } as unknown as Channel;
  loadAnimation.mockClear();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete store.state.sync.channelsById[1];
});

describe('стикеры', () => {
  it('разбор: формат и эмодзи', () => {
    const [att] = parseAttachments(sticker('animated'));
    expect(att).toMatchObject({ type: 'sticker', stickerFormat: 'animated', emoji: '😀' });
    const [bad] = parseAttachments(JSON.stringify([{ type: 'sticker', sticker_format: 'x' }]));
    expect(bad!.stickerFormat).toBeUndefined();
  });

  it('.webp — <img> 160 px', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ attachments_json: sticker('static'), media: [item({ id: 5 })] }) },
    });
    const img = wrapper.get('.sticker img');
    expect(img.attributes('src')).toBe('/api/v1/media/5');
    expect(wrapper.find('.media-thumb').exists()).toBe(false);
  });

  it('.webm — видео без звука по кругу', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ attachments_json: sticker('video'), media: [item({ id: 6, detected: 'webm' })] }),
      },
    });
    const video = wrapper.get('.sticker video');
    expect(video.attributes('src')).toBe('/api/v1/media/6');
    expect(video.attributes('autoplay')).toBeDefined();
    expect(video.attributes('loop')).toBeDefined();
    expect(video.attributes('playsinline')).toBeDefined();
    expect((video.element as HTMLVideoElement).muted).toBe(true);
  });

  it('.tgs — gzip распаковывается и уходит в Lottie (лениво)', async () => {
    const json = JSON.stringify({ v: '5.5.2', layers: [] });
    const gz = new Uint8Array(gzipSync(Buffer.from(json)));
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(gz, { status: 200 })),
    );
    const wrapper = mount(MessageRow, {
      attachTo: document.body,
      props: {
        message: msg({ attachments_json: sticker('animated'), media: [item({ id: 7, detected: 'other' })] }),
      },
    });
    await vi.waitFor(() => expect(loadAnimation).toHaveBeenCalledTimes(1));
    const arg = (loadAnimation.mock.calls[0] as unknown as [{ animationData: unknown; renderer: string }])[0];
    expect(arg.animationData).toEqual({ v: '5.5.2', layers: [] });
    expect(arg.renderer).toBe('svg');
    wrapper.unmount();
  });

  it('ошибка загрузки .tgs — текст «стикер 😀»', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('x', { status: 404 })),
    );
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ attachments_json: sticker('animated'), media: [item({ id: 8, detected: 'other' })] }),
      },
    });
    await flushPromises();
    await vi.waitFor(() => expect(wrapper.get('.sticker__text').text()).toBe('стикер 😀'));
    expect(loadAnimation).not.toHaveBeenCalled();
  });

  it('медиа не скачано — подпись «Стикер 😀» и «Загрузить», не файл', () => {
    const [att] = parseAttachments(sticker('animated'));
    expect(displayFor(att!, item({ status: 'deferred' }), 'telegram')).toBe('fetch');
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({ attachments_json: sticker('animated'), media: [item({ status: 'deferred' })] }),
      },
    });
    expect(wrapper.text()).toContain('Стикер 😀');
    expect(wrapper.find('.sticker').exists()).toBe(false);
  });
});
