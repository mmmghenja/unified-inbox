// Ф10-4 C (`docs/architecture.md`): файлы в поле ввода — скрепка, вставка из буфера,
// перетаскивание добавляют файл и сразу грузят его (`store.uploadFile`); превью картинки — `data:`; три файла ->
// три `send` по порядку, у каждого свой ключ, текст — подписью к первому и убирается после его успеха; ошибка
// загрузки/отправки — строка под полем, неотправленное остаётся; у почты скрепки нет. API-клиент: загрузка —
// сырые байты с `X-Filename`, `files` в `send` — только когда есть.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, UploadResult } from '../src/api/types';
import Composer from '../src/components/Composer.vue';
import { store } from '../src/store/instance';

const PNG_BYTES = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);

function channel(source: string): Channel {
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
  };
}

function png(name = 'shot.png'): File {
  return new File([PNG_BYTES], name, { type: 'image/png' });
}

function pdf(name = 'doc.pdf'): File {
  return new File(['%PDF-1.7'], name, { type: 'application/pdf' });
}

let uploadSpy: MockInstance;
let sendSpy: MockInstance;
let counter = 0;
let wrapper: VueWrapper | null = null;

function uploaded(name: string): UploadResult {
  counter += 1;
  const kind = name.endsWith('.png') ? 'photo' : 'document';
  return { upload_id: `u${counter}`, size: 10, mime: kind === 'photo' ? 'image/png' : 'application/pdf', name, kind };
}

function mountComposer(source = 'telegram', props: Record<string, unknown> = {}): VueWrapper {
  store.state.sync.channelsById = { 1: channel(source) };
  wrapper = mount(Composer, { props: { channelId: 1, ...props }, attachTo: document.body });
  return wrapper;
}

async function pick(w: VueWrapper, files: File[]): Promise<void> {
  const input = w.get('input[type=file]');
  Object.defineProperty(input.element, 'files', { value: files, configurable: true });
  await input.trigger('change');
  await flushPromises();
}

/** FileReader в jsdom читает асинхронно — ждём реальный тик, пока превью не встанет. */
async function waitFor(check: () => boolean): Promise<void> {
  for (let i = 0; i < 50 && !check(); i++) {
    await new Promise((resolve) => setTimeout(resolve, 5));
    await flushPromises();
  }
}

beforeEach(() => {
  counter = 0;
  uploadSpy = vi
    .spyOn(store, 'uploadFile')
    .mockImplementation(async (_channelId: number, _file: Blob, name: string) => uploaded(name));
  sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  store.state.sync.channelsById = {};
});

describe('35 Ф10-4: скрепка по источнику', () => {
  it.each(['telegram', 'slack'])('%s — скрепка есть', (source) => {
    const w = mountComposer(source);
    expect(w.find('.composer-attach').exists()).toBe(true);
    expect(w.find('input[type=file]').exists()).toBe(true);
  });

  it('источник без файлов — скрепки нет, вставка файла ничего не грузит', async () => {
    // Ф11-3b C1: почта теперь со скрепкой (тест 45); «нет скрепки» — у источника, куда файлы не уходят.
    const w = mountComposer('pyrus');
    expect(w.find('.composer-attach').exists()).toBe(false);
    expect(w.find('input[type=file]').exists()).toBe(false);
    await w.get('textarea').trigger('paste', { clipboardData: { files: [png()] } });
    await flushPromises();
    expect(uploadSpy).not.toHaveBeenCalled();
    expect(w.find('.composer-file').exists()).toBe(false);
  });
});

describe('35 Ф10-4: добавление файла', () => {
  it('скрепка: выбранный файл добавлен и загружен в ядро к этой беседе', async () => {
    const w = mountComposer();
    await pick(w, [pdf()]);
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(uploadSpy.mock.calls[0]![0]).toBe(1);
    expect(uploadSpy.mock.calls[0]![2]).toBe('doc.pdf');
    expect(w.get('.composer-file-name').text()).toBe('doc.pdf');
    expect(w.find('.composer-file-ready').exists()).toBe(true);
  });

  it('переход в другую беседу — прикреплённое не едет следом (приёмка, находка ревью)', async () => {
    const w = mountComposer();
    await pick(w, [pdf()]);
    expect(w.find('.composer-file-name').exists()).toBe(true);
    store.state.sync.channelsById = { 1: channel('telegram'), 2: channel('telegram') };
    await w.setProps({ channelId: 2 });
    expect(w.find('.composer-file-name').exists()).toBe(false);
  });

  it('вставка из буфера: файл добавлен, вставка текста не сорвана для обычного текста', async () => {
    const w = mountComposer();
    await w.get('textarea').trigger('paste', { clipboardData: { files: [png()] } });
    await flushPromises();
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(w.findAll('.composer-file')).toHaveLength(1);

    await w.get('textarea').trigger('paste', { clipboardData: { files: [] } });
    await flushPromises();
    expect(uploadSpy).toHaveBeenCalledTimes(1);
  });

  it('перетаскивание на поле: файл добавлен', async () => {
    const w = mountComposer();
    await w.get('form').trigger('dragover', { dataTransfer: { types: ['Files'], files: [] } });
    expect(w.get('form').classes()).toContain('composer-drag');
    await w.get('form').trigger('drop', { dataTransfer: { types: ['Files'], files: [pdf('a.pdf')] } });
    await flushPromises();
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(w.get('.composer-file-name').text()).toBe('a.pdf');
    expect(w.get('form').classes()).not.toContain('composer-drag');
  });

  it('превью картинки — data:, у документа — значок', async () => {
    const w = mountComposer();
    await pick(w, [png(), pdf()]);
    await waitFor(() => w.find('img.composer-file-preview').exists());
    const img = w.get('img.composer-file-preview');
    expect(img.attributes('src')).toMatch(/^data:image\/png;base64,/);
    expect(w.findAll('.composer-file-icon')).toHaveLength(1);
  });

  it('пока грузится — «загружается…», отправить нельзя', async () => {
    let release: (value: UploadResult) => void = () => {};
    uploadSpy.mockImplementation(
      () =>
        new Promise<UploadResult>((resolve) => {
          release = resolve;
        }),
    );
    const w = mountComposer();
    await pick(w, [pdf()]);
    expect(w.get('.composer-file-state').text()).toBe('загружается…');
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined();
    release(uploaded('doc.pdf'));
    await flushPromises();
    expect(w.get('button[type=submit]').attributes('disabled')).toBeUndefined();
  });

  it('не больше 10 файлов — лишние не грузятся, строка под полем', async () => {
    const w = mountComposer();
    await pick(
      w,
      Array.from({ length: 11 }, (_, i) => pdf(`f${i}.pdf`)),
    );
    expect(uploadSpy).toHaveBeenCalledTimes(10);
    expect(w.get('.composer-error').text()).toContain('10');
  });

  it('крестик убирает файл до отправки', async () => {
    const w = mountComposer();
    await pick(w, [pdf()]);
    await w.get('.composer-file-remove').trigger('click');
    expect(w.find('.composer-file').exists()).toBe(false);
  });
});

describe('35 Ф10-4: отправка', () => {
  it('Ф11-3b: картинка и два PDF в Telegram -> две пачки по порядку, текст — с первой, свои ключи, поле очищено', async () => {
    const w = mountComposer();
    await pick(w, [png('a.png'), pdf('b.pdf'), pdf('c.pdf')]);
    await w.get('textarea').setValue('  подпись  ');
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();

    expect(sendSpy).toHaveBeenCalledTimes(2);
    const calls = sendSpy.mock.calls;
    expect(calls.map((c) => c[1])).toEqual(['подпись', '']);
    expect(calls.map((c) => (c[3] as { files: string[] }).files)).toEqual([['u1'], ['u2', 'u3']]);
    const keys = calls.map((c) => c[2]);
    expect(new Set(keys).size).toBe(2);
    expect(w.find('.composer-file').exists()).toBe(false);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('');
    expect(w.find('.composer-error').exists()).toBe(false);
  });

  it('ответ — только у первой пачки, плашка снимается', async () => {
    const w = mountComposer('telegram', { replyTo: { extId: '77', preview: 'Вася: привет' } });
    await pick(w, [png('a.png'), pdf('b.pdf')]);
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();
    expect(sendSpy.mock.calls.map((c) => (c[3] as { replyTo?: string }).replyTo)).toEqual(['77', undefined]);
    expect(w.emitted('cancel-reply')).toHaveLength(1);
  });

  it('Ctrl+Enter отправляет файл без текста', async () => {
    const w = mountComposer('slack', { thread: '1700.1' });
    await pick(w, [pdf()]);
    await w.get('textarea').trigger('keydown', { key: 'Enter', ctrlKey: true });
    await flushPromises();
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]![1]).toBe('');
    expect((sendSpy.mock.calls[0]![3] as { thread?: string }).thread).toBe('1700.1');
  });

  it('сбой второй пачки: первая ушла и убрана, текст убран, вторая осталась, строка ошибки', async () => {
    sendSpy
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new api.NetworkError(new Error('down')));
    const w = mountComposer();
    await pick(w, [png('a.png'), pdf('b.pdf'), pdf('c.pdf')]);
    await w.get('textarea').setValue('подпись');
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();

    expect(sendSpy).toHaveBeenCalledTimes(2);
    expect(w.findAll('.composer-file-name').map((n) => n.text())).toEqual(['b.pdf', 'c.pdf']);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('');
    expect(w.get('.composer-error').text()).toContain('Не удалось отправить');

    // Повтор — тем же ключом второй пачки и без подписи (она уже ушла с первой).
    const failedKey = sendSpy.mock.calls[1]![2];
    sendSpy.mockResolvedValue(undefined);
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();
    expect(sendSpy.mock.calls[2]![2]).toBe(failedKey);
    expect(sendSpy.mock.calls[2]![1]).toBe('');
    expect((sendSpy.mock.calls[2]![3] as { files: string[] }).files).toEqual(['u2', 'u3']);
    expect(w.find('.composer-file').exists()).toBe(false);
  });

  it('сбой первой отправки: текст и файлы остаются в поле', async () => {
    sendSpy.mockRejectedValueOnce(new api.ApiError(422, 'x', null));
    const w = mountComposer();
    await pick(w, [pdf('a.pdf')]);
    await w.get('textarea').setValue('подпись');
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('подпись');
    expect(w.findAll('.composer-file')).toHaveLength(1);
    expect(w.get('.composer-error').text()).toContain('Не удалось отправить');
  });

  it('ошибка загрузки — строка у файла и под полем, отправить нельзя до «убрать»', async () => {
    uploadSpy.mockRejectedValueOnce(new api.ApiError(413, 'too big', null));
    const w = mountComposer();
    await pick(w, [pdf('big.pdf')]);
    expect(w.get('.composer-file-state').text()).toBe('файл больше 50 МиБ');
    expect(w.get('.composer-error').text()).toContain('не загрузился');
    await w.get('textarea').setValue('текст');
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined();
    await w.get('.composer-file-remove').trigger('click');
    expect(w.get('button[type=submit]').attributes('disabled')).toBeUndefined();
  });

  it('без файлов — как было: один send с текстом, без files', async () => {
    const w = mountComposer();
    await w.get('textarea').setValue('привет');
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]![1]).toBe('привет');
    expect((sendSpy.mock.calls[0]![3] as { files?: string[] }).files).toBeUndefined();
  });
});

describe('35 Ф10-4: API-клиент', () => {
  it('uploadFile: POST /uploads?channel_id=, сырые байты, X-Filename urlencoded, CSRF', async () => {
    api.setCsrf('csrf-1');
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ upload_id: 'u', size: 3, mime: 'image/png', name: 'отчёт.png', kind: 'photo' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const file = png('отчёт.png');
    const result = await api.uploadFile(5, file, 'отчёт.png');
    expect(result.upload_id).toBe('u');
    const [url, init] = fetchMock.mock.calls[0]! as [string, RequestInit & { headers: Record<string, string> }];
    expect(url).toBe('/api/v1/uploads?channel_id=5');
    expect(init.method).toBe('POST');
    expect(init.body).toBe(file);
    expect(init.headers['X-Filename']).toBe(encodeURIComponent('отчёт.png'));
    expect(init.headers['X-Hub-CSRF']).toBe('csrf-1');
    expect(init.headers['X-Hub-Client']).toBe('hub-ui');
    expect(init.credentials).toBe('same-origin');
  });

  it('uploadFile: 413 -> ApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'big' }), {
          status: 413,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    );
    await expect(api.uploadFile(5, png(), 'a.png')).rejects.toMatchObject({ status: 413 });
  });

  it('sendMessage: files — только когда переданы', async () => {
    const ok = () =>
      new Response(JSON.stringify({ outbox_id: 1, message_id: 2, status: 'queued', created: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    const fetchMock = vi.fn().mockImplementation(async () => ok());
    vi.stubGlobal('fetch', fetchMock);
    await api.sendMessage({ channelId: 1, text: '', idempotencyKey: 'k', files: ['u1'] });
    await api.sendMessage({ channelId: 1, text: 'x', idempotencyKey: 'k2' });
    const body1 = JSON.parse(fetchMock.mock.calls[0]![1].body as string);
    const body2 = JSON.parse(fetchMock.mock.calls[1]![1].body as string);
    expect(body1.files).toEqual(['u1']);
    expect(body1.text).toBe('');
    expect('files' in body2).toBe(false);
  });
});
