// Ф11-3b C (`docs/architecture.md`): прикреплённое уходит пачками — одна
// пачка = один `send` с `files: [id…]`. Telegram — альбом одного вида (картинки или файлы), не больше 10 и 50 МиБ;
// Slack — не больше 10 и 50 МиБ; почта — всегда одно письмо, больше 17 МиБ — ошибка под полем без запроса.
// Скрепка — и в беседах почты. Ф14-2 D4: и в беседе-заготовке любого источника с файлами (тест 16 — в 48).
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, UploadResult } from '../src/api/types';
import Composer from '../src/components/Composer.vue';
import { store } from '../src/store/instance';
import { MAIL_MAX_BYTES, splitFileBatches } from '../src/util/fileBatches';

const MIB = 1024 * 1024;

function channel(source: string, extra: Partial<Channel> = {}): Channel {
  return {
    id: 1,
    ext_id: 'c1',
    kind: 'dm',
    title: 'Канал',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 1,
    source,
    ...extra,
  };
}

function file(name: string): File {
  return new File(['x'], name, { type: name.endsWith('.png') ? 'image/png' : 'application/pdf' });
}

let sendSpy: MockInstance;
let counter = 0;
let sizes: Record<string, number> = {};
let wrapper: VueWrapper | null = null;

function uploaded(name: string): UploadResult {
  counter += 1;
  const kind = name.endsWith('.png') ? 'photo' : 'document';
  return { upload_id: `u${counter}`, size: sizes[name] ?? 10, mime: 'x/y', name, kind };
}

function mountComposer(ch: Channel, props: Record<string, unknown> = {}): VueWrapper {
  store.state.sync.channelsById = { 1: ch };
  wrapper = mount(Composer, { props: { channelId: 1, ...props }, attachTo: document.body });
  return wrapper;
}

async function pick(w: VueWrapper, files: File[]): Promise<void> {
  const input = w.get('input[type=file]');
  Object.defineProperty(input.element, 'files', { value: files, configurable: true });
  await input.trigger('change');
  await flushPromises();
}

async function submit(w: VueWrapper): Promise<void> {
  await w.get('button[type=submit]').trigger('click');
  await flushPromises();
}

function sentFiles(): string[][] {
  return sendSpy.mock.calls.map((c) => (c[3] as { files: string[] }).files);
}

beforeEach(() => {
  counter = 0;
  sizes = {};
  vi.spyOn(store, 'uploadFile').mockImplementation(async (_c: number, _f: Blob, name: string) => uploaded(name));
  sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.restoreAllMocks();
  store.state.sync.channelsById = {};
});

describe('45 Ф11-3b: пачки в Composer (тест 18)', () => {
  it('пять картинок в Telegram -> один send с пятью id и подписью', async () => {
    const w = mountComposer(channel('telegram'));
    await pick(w, [1, 2, 3, 4, 5].map((i) => file(`p${i}.png`)));
    await w.get('textarea').setValue('альбом');
    await submit(w);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]![1]).toBe('альбом');
    expect(sentFiles()).toEqual([['u1', 'u2', 'u3', 'u4', 'u5']]);
    expect(w.find('.composer-file').exists()).toBe(false);
  });

  it('три картинки + PDF в Telegram -> два send: картинки, затем PDF; подпись и ответ — только в первом', async () => {
    const w = mountComposer(channel('telegram'), { replyTo: { extId: '77', preview: 'Вася: привет' } });
    await pick(w, [file('a.png'), file('b.png'), file('c.png'), file('d.pdf')]);
    await w.get('textarea').setValue('подпись');
    await submit(w);
    expect(sentFiles()).toEqual([['u1', 'u2', 'u3'], ['u4']]);
    expect(sendSpy.mock.calls.map((c) => c[1])).toEqual(['подпись', '']);
    expect(sendSpy.mock.calls.map((c) => (c[3] as { replyTo?: string }).replyTo)).toEqual(['77', undefined]);
    expect(new Set(sendSpy.mock.calls.map((c) => c[2])).size).toBe(2);
    expect(w.emitted('cancel-reply')).toHaveLength(1);
  });

  it('Slack: картинки и PDF вперемешку — одно сообщение', async () => {
    const w = mountComposer(channel('slack'), { thread: '1700.1' });
    await pick(w, [file('a.png'), file('b.pdf'), file('c.png')]);
    await submit(w);
    expect(sentFiles()).toEqual([['u1', 'u2', 'u3']]);
    expect((sendSpy.mock.calls[0]![3] as { thread?: string }).thread).toBe('1700.1');
  });

  it('почта: три файла -> один send (письмо только с вложениями)', async () => {
    const w = mountComposer(channel('mail', { kind: 'mail_thread' }));
    await pick(w, [file('a.pdf'), file('b.png'), file('c.pdf')]);
    await submit(w);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]![1]).toBe('');
    expect(sentFiles()).toEqual([['u1', 'u2', 'u3']]);
  });

  it('почта: 19 МиБ суммой -> ошибка под полем, запроса нет, файлы и текст на месте', async () => {
    sizes = { 'a.pdf': 7 * MIB, 'b.pdf': 6 * MIB, 'c.pdf': 6 * MIB };
    const w = mountComposer(channel('mail', { kind: 'mail_thread' }));
    await pick(w, [file('a.pdf'), file('b.pdf'), file('c.pdf')]);
    await w.get('textarea').setValue('ответ');
    await submit(w);
    expect(sendSpy).not.toHaveBeenCalled();
    expect(w.get('.composer-error').text()).toContain('17 МиБ');
    expect(w.findAll('.composer-file')).toHaveLength(3);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('ответ');

    await w.findAll('.composer-file-remove')[2]!.trigger('click');
    await submit(w);
    expect(sentFiles()).toEqual([['u1', 'u2']]); // 13 МиБ — уходит
  });

  it('сбой второй пачки: первая убрана из поля, вторая осталась, подписи у повтора нет', async () => {
    sendSpy.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new api.ApiError(500, 'x', null));
    const w = mountComposer(channel('telegram'));
    await pick(w, [file('a.png'), file('b.png'), file('c.pdf')]);
    await w.get('textarea').setValue('подпись');
    await submit(w);
    expect(sentFiles()).toEqual([['u1', 'u2'], ['u3']]);
    expect(w.findAll('.composer-file-name').map((n) => n.text())).toEqual(['c.pdf']);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('');
    expect(w.get('.composer-error').text()).toContain('Не удалось отправить');
  });

  it('правка текста после сбоя — у пачки новый ключ', async () => {
    sendSpy.mockRejectedValueOnce(new api.NetworkError(new Error('down')));
    const w = mountComposer(channel('slack'));
    await pick(w, [file('a.pdf'), file('b.pdf')]);
    await w.get('textarea').setValue('раз');
    await submit(w);
    const firstKey = sendSpy.mock.calls[0]![2];
    await submit(w);
    expect(sendSpy.mock.calls[1]![2]).toBe(firstKey); // повтор той же пачки — тот же ключ
    sendSpy.mockRejectedValueOnce(new api.NetworkError(new Error('down')));
    await submit(w);
    await w.get('textarea').setValue('два');
    await w.get('textarea').trigger('input');
    await submit(w);
    expect(sendSpy.mock.calls.at(-1)![2]).not.toBe(firstKey);
  });
});

describe('45 Ф11-3b: скрепка у почты (тест 19)', () => {
  it('беседа почты — скрепка есть', () => {
    const w = mountComposer(channel('mail', { kind: 'mail_thread' }));
    expect(w.find('.composer-attach').exists()).toBe(true);
  });

  // Ф12-5 C3: новое письмо уходит с вложениями одной пачкой — у почтовой заготовки скрепка есть (у Telegram — нет).
  it('беседа-заготовка нового письма — скрепка есть', () => {
    const w = mountComposer(
      channel('mail', {
        kind: 'mail_thread',
        ext_id: 'new:0123456789abcdef0123456789abcdef',
        new_to: { kind: 'email', to: 'a@b.c', subject: 'Тема', from: null },
      }),
    );
    expect(w.find('.composer-attach').exists()).toBe(true);
  });

  it('Ф14-2 D4: заготовка нового адресата Telegram — скрепка есть', () => {
    const w = mountComposer(
      channel('telegram', { ext_id: 'new:ffff', new_to: { kind: 'tg_username', username: 'petr' } }),
    );
    expect(w.find('.composer-attach').exists()).toBe(true);
  });

  it('ошибка загрузки 413 в беседе почты — про 17 МиБ', async () => {
    vi.spyOn(store, 'uploadFile').mockRejectedValueOnce(new api.ApiError(413, 'big', null));
    const w = mountComposer(channel('mail', { kind: 'mail_thread' }));
    await pick(w, [file('big.pdf')]);
    expect(w.get('.composer-file-state').text()).toContain('17 МиБ');
  });
});

describe('45 Ф11-3b: splitFileBatches', () => {
  const item = (kind: string, size = 10) => ({ kind, size });

  it('12 файлов в Slack -> 10 + 2', () => {
    const items = Array.from({ length: 12 }, () => item('document'));
    const result = splitFileBatches(items, 'slack');
    expect('batches' in result && result.batches.map((b) => b.length)).toEqual([10, 2]);
  });

  it('больше 50 МиБ суммой — новая пачка; ровно 50 — одна', () => {
    const exact = splitFileBatches([item('document', 25 * MIB), item('document', 25 * MIB)], 'slack');
    expect('batches' in exact && exact.batches.map((b) => b.length)).toEqual([2]);
    const over = splitFileBatches([item('document', 25 * MIB), item('document', 25 * MIB + 1)], 'fake');
    expect('batches' in over && over.batches.map((b) => b.length)).toEqual([1, 1]);
  });

  it('Telegram: новая пачка при смене вида, порядок поля сохраняется', () => {
    const items = [item('photo'), item('photo'), item('document'), item('photo')];
    const result = splitFileBatches(items, 'telegram');
    expect('batches' in result && result.batches.map((b) => b.map((i) => i.kind))).toEqual([
      ['photo', 'photo'],
      ['document'],
      ['photo'],
    ]);
  });

  it('почта: одна пачка до 17 МиБ, больше — ошибка', () => {
    const ok = splitFileBatches([item('document', MAIL_MAX_BYTES)], 'mail');
    expect('batches' in ok && ok.batches.length).toBe(1);
    const over = splitFileBatches([item('document', MAIL_MAX_BYTES), item('photo', 1)], 'mail');
    expect('error' in over && over.error).toContain('17 МиБ');
    expect(MAIL_MAX_BYTES).toBe(17 * MIB);
  });
});
