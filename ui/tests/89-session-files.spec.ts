// Ф41-3в (`docs/architecture.md`, интерфейс): файлы и картинки во вход сессии — выбор,
// вставка из буфера, перетаскивание, чипы с размером и «×» (DELETE), запрет отправки при загрузке, ошибка 413,
// `say` с `files` (в том числе с пустым текстом), очистка после отправки, строка «📎» в ленте. Ответы ядра — строго
// по контракту 4.1.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, SessionEvent, SessionFileResult } from '../src/api/types';
import SessionWindow from '../src/components/SessionWindow.vue';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { stepView } from '../src/util/sessionSteps';

const NOW = new Date(2026, 10, 8, 15, 0, 0).getTime();

interface Sent {
  method: string;
  path: string;
  params: URLSearchParams;
  body: unknown;
  signal?: AbortSignal | null;
}

interface Stub {
  sent: Sent[];
  /** Ответ на загрузку; по умолчанию — успех с kind по имени. Можно подменить на 413 / повисший запрос. */
  upload: (name: string, sent: Sent) => Promise<Response> | Response;
  say: () => Response;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let nextFile = 0;
function okUpload(name: string, size = 5): Response {
  const kind: SessionFileResult['kind'] = /\.(png|jpe?g)$/.test(name) ? 'image' : /\.pdf$/.test(name) ? 'pdf' : 'file';
  const result: SessionFileResult = { id: `f${++nextFile}`, name, size, mime: 'x/y', kind };
  return json(result);
}

function stubFetch(): Stub {
  const stub: Stub = {
    sent: [],
    upload: (name) => okUpload(name),
    say: () => json({ id: 1, session_id: 5, priority: 'next', status: 'queued' }),
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const isRaw = init?.body instanceof Blob;
      const sent: Sent = {
        method,
        path: url.pathname,
        params: url.searchParams,
        body: isRaw ? init!.body : init?.body ? JSON.parse(String(init.body)) : undefined,
        signal: init?.signal,
      };
      stub.sent.push(sent);
      if (method === 'POST' && url.pathname === '/api/v1/sessions/5/files') {
        return stub.upload(url.searchParams.get('name') ?? '', sent);
      }
      if (method === 'DELETE') return json(null);
      if (method === 'POST' && url.pathname === '/api/v1/sessions/5/say') return stub.say();
      if (/\/events$/.test(url.pathname)) return json([]);
      if (/^\/api\/v1\/sessions\/\d+$/.test(url.pathname)) return json({ session: session(), requests: [], says: [] });
      if (url.pathname === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      return json({});
    }),
  );
  return stub;
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5,
    project_id: 3,
    role: 'программист',
    role_no: 1,
    engine: 'claude',
    model: 'sonnet',
    effort: null,
    mode: 'manual',
    title: null,
    state: 'running',
    host_seen_ts: NOW,
    created_ts: NOW - 1000,
    updated_ts: NOW - 500,
    closed_ts: null,
    close_reason: null,
    created_by: 'ui',
    seq: 10,
    last_event_id: 20,
    last_turn_ts: null,
    failure: null,
    wait_reason: null,
    pending_requests: 0,
    queued_says: 0,
    interrupt_requested: false,
    sleep_requested: false,
    sub_id: null,
    listening: false,
    engine_session_id: null,
    workdir: '/home/owner/proj',
    cache: null,
    usage: null,
    ...overrides,
  };
}

function mountWindow(events: SessionEvent[] = []) {
  const s = session();
  store.state.sessions.items[s.id] = s;
  store.state.sessions.events[s.id] = events;
  return mount(SessionWindow, { props: { session: s } });
}

function file(name: string, content = 'hello', type = 'text/plain'): File {
  return new File([content], name, { type });
}

async function pick(w: ReturnType<typeof mountWindow>, files: File[]): Promise<void> {
  const input = w.get('input[type="file"]');
  Object.defineProperty(input.element, 'files', { configurable: true, value: files });
  await input.trigger('change');
  await flushPromises();
}

function uploads(stub: Stub): Sent[] {
  return stub.sent.filter((r) => r.method === 'POST' && r.path === '/api/v1/sessions/5/files');
}

function says(stub: Stub): Sent[] {
  return stub.sent.filter((r) => r.method === 'POST' && r.path === '/api/v1/sessions/5/say');
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sessions = createSessionsState();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-3в: приложить файл', () => {
  it('кнопка «📎 Файл» + выбор: POST сырых байтов с именем в query; чип с именем и размером; подсказка у kind=file', async () => {
    const stub = stubFetch();
    const w = mountWindow();
    await flushPromises();
    expect(w.get('.session-attach').text()).toBe('📎 Файл');
    expect(w.get('input[type="file"]').attributes('multiple')).toBeDefined();
    await pick(w, [file('notes.txt'), file('shot.png', 'png!', 'image/png')]);
    const up = uploads(stub);
    expect(up).toHaveLength(2);
    expect(up[0]!.params.get('name')).toBe('notes.txt');
    expect(up[0]!.body).toBeInstanceOf(Blob);
    const chips = w.findAll('.session-file');
    expect(chips).toHaveLength(2);
    expect(chips[0]!.text()).toContain('notes.txt');
    expect(chips[0]!.text()).toContain('5 Б');
    expect(chips[0]!.text()).toContain('модель получит путь к файлу в папке проекта');
    expect(chips[1]!.text()).not.toContain('модель получит путь');
    w.unmount();
  });

  it('вставка картинки из буфера (Ctrl+V с файлом) — загрузка; обычный текст вставляется как всегда', async () => {
    const stub = stubFetch();
    const w = mountWindow();
    await flushPromises();
    const prevented = vi.fn();
    await w.get('textarea').trigger('paste', { clipboardData: { files: [file('image.png', 'x', 'image/png')] }, preventDefault: prevented });
    await flushPromises();
    expect(uploads(stub)).toHaveLength(1);
    expect(uploads(stub)[0]!.params.get('name')).toBe('image.png');
    expect(w.findAll('.session-file')).toHaveLength(1);
    await w.get('textarea').trigger('paste', { clipboardData: { files: [], getData: () => 'просто текст' } });
    await flushPromises();
    expect(uploads(stub)).toHaveLength(1);
    w.unmount();
  });

  it('перетаскивание файлов на окно — загрузка', async () => {
    const stub = stubFetch();
    const w = mountWindow();
    await flushPromises();
    await w.trigger('dragover', { dataTransfer: { types: ['Files'] } });
    expect(w.classes()).toContain('session-window--drop');
    await w.trigger('drop', { dataTransfer: { types: ['Files'], files: [file('drop.pdf', '%PDF', 'application/pdf')] } });
    await flushPromises();
    expect(w.classes()).not.toContain('session-window--drop');
    expect(uploads(stub)).toHaveLength(1);
    expect(w.get('.session-file').text()).toContain('drop.pdf');
    w.unmount();
  });

  it('больше 10 файлов за раз — лишние отбрасываются со строкой', async () => {
    const stub = stubFetch();
    const w = mountWindow();
    await flushPromises();
    await pick(w, Array.from({ length: 11 }, (_, i) => file(`f${i}.txt`)));
    expect(uploads(stub)).toHaveLength(10);
    expect(w.get('[role="alert"]').text()).toMatch(/Не больше 10/);
    w.unmount();
  });
});

describe('Ф41-3в: загрузка, ошибка, «×»', () => {
  it('пока файл грузится — «Отправить» и «Прервать и отправить» неактивны; загрузился — активны и при пустом тексте', async () => {
    const stub = stubFetch();
    let finish: (r: Response) => void = () => undefined;
    stub.upload = () => new Promise<Response>((resolve) => (finish = resolve));
    const w = mountWindow();
    await flushPromises();
    await w.get('textarea').setValue('привет');
    expect(w.get('.work-say-next').attributes('disabled')).toBeUndefined();
    await pick(w, [file('a.txt')]);
    expect(w.get('.session-file').text()).toContain('загружается');
    expect(w.get('.work-say-next').attributes('disabled')).toBeDefined();
    expect(w.get('.work-say-now').attributes('disabled')).toBeDefined();
    await w.get('textarea').setValue('');
    finish(okUpload('a.txt'));
    await flushPromises();
    expect(w.get('.work-say-next').attributes('disabled')).toBeUndefined(); // текста нет, но файл готов
    expect(w.get('.work-say-now').attributes('disabled')).toBeUndefined();
    w.unmount();
  });

  it('413 — «файл больше 20 МБ» у чипа, чип остаётся до «×», отправить нельзя', async () => {
    const stub = stubFetch();
    stub.upload = () => json({ detail: 'too big' }, 413);
    const w = mountWindow();
    await flushPromises();
    await pick(w, [file('big.bin')]);
    expect(w.get('.session-file-error').text()).toBe('файл больше 20 МБ');
    expect(w.get('.work-say-next').attributes('disabled')).toBeDefined();
    await w.get('.session-file-remove').trigger('click');
    await flushPromises();
    expect(w.find('.session-file').exists()).toBe(false);
    expect(stub.sent.some((r) => r.method === 'DELETE')).toBe(false); // загруженного нет — удалять нечего
    w.unmount();
  });

  it('409 при загрузке — строка ядра как есть', async () => {
    const stub = stubFetch();
    stub.upload = () => json({ detail: 'слишком много неприложенных файлов' }, 409);
    const w = mountWindow();
    await flushPromises();
    await pick(w, [file('a.txt')]);
    expect(w.get('.session-file-error').text()).toBe('слишком много неприложенных файлов');
    w.unmount();
  });

  it('«×» на загруженном — DELETE /sessions/5/files/{id}; на грузящемся — отмена запроса', async () => {
    const stub = stubFetch();
    const w = mountWindow();
    await flushPromises();
    await pick(w, [file('a.txt')]);
    await w.get('.session-file-remove').trigger('click');
    await flushPromises();
    const del = stub.sent.filter((r) => r.method === 'DELETE');
    expect(del).toHaveLength(1);
    expect(del[0]!.path).toMatch(/^\/api\/v1\/sessions\/5\/files\/f\d+$/);
    expect(w.find('.session-file').exists()).toBe(false);

    stub.upload = (_name, sent) =>
      new Promise<Response>((_, reject) => sent.signal?.addEventListener('abort', () => reject(new DOMException('a', 'AbortError'))));
    await pick(w, [file('slow.txt')]);
    const slow = uploads(stub)[1]!;
    expect(slow.signal?.aborted).toBe(false);
    await w.get('.session-file-remove').trigger('click');
    await flushPromises();
    expect(slow.signal?.aborted).toBe(true);
    expect(w.find('.session-file').exists()).toBe(false);
    expect(stub.sent.filter((r) => r.method === 'DELETE')).toHaveLength(1);
    w.unmount();
  });

  it('чипы живут в сторе: закрыл и открыл окно — на месте', async () => {
    stubFetch();
    const w = mountWindow();
    await flushPromises();
    await pick(w, [file('keep.txt')]);
    w.unmount();
    const again = mountWindow();
    await flushPromises();
    expect(again.get('.session-file').text()).toContain('keep.txt');
    again.unmount();
  });
});

describe('Ф41-3в: отправка', () => {
  it('say с files; пустой текст разрешён; чипы очищаются после успеха', async () => {
    const stub = stubFetch();
    const w = mountWindow();
    await flushPromises();
    await pick(w, [file('a.txt'), file('b.pdf', '%PDF', 'application/pdf')]);
    const ids = store.state.sessions.files[5]!.map((f) => f.fileId);
    await w.get('.work-say-next').trigger('click');
    await flushPromises();
    expect(says(stub)).toHaveLength(1);
    expect(says(stub)[0]!.body).toEqual({ text: '', priority: 'next', files: ids });
    expect(w.find('.session-file').exists()).toBe(false);
    w.unmount();
  });

  it('текст вместе с файлом — одной репликой; без файлов поле files не уходит', async () => {
    const stub = stubFetch();
    const w = mountWindow();
    await flushPromises();
    await w.get('textarea').setValue('смотри');
    await w.get('.work-say-next').trigger('click');
    await flushPromises();
    expect(says(stub)[0]!.body).toEqual({ text: 'смотри', priority: 'next' });
    await pick(w, [file('a.txt')]);
    await w.get('textarea').setValue('вот файл');
    await w.get('.work-say-now').trigger('click');
    await flushPromises();
    expect(says(stub)[1]!.body).toMatchObject({ text: 'вот файл', priority: 'now', files: [expect.any(String)] });
    w.unmount();
  });

  it('say не прошёл (422) — чипы и текст остаются, строка ядра как есть', async () => {
    const stub = stubFetch();
    stub.say = () => json({ detail: 'files: файл не найден или уже отправлен' }, 422);
    const w = mountWindow();
    await flushPromises();
    await pick(w, [file('a.txt')]);
    await w.get('textarea').setValue('текст');
    await w.get('.work-say-next').trigger('click');
    await flushPromises();
    expect(w.findAll('.session-file')).toHaveLength(1);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('текст');
    expect(w.get('.work-error').text()).toBe('files: файл не найден или уже отправлен');
    w.unmount();
  });
});

describe('Ф41-3в: лента', () => {
  const sayEvent = (summary: string, files: { name: string; size: number; kind: string }[]): SessionEvent => ({
    id: 7,
    ts: NOW,
    kind: 'say',
    tool: null,
    summary,
    is_error: false,
    ref: '3',
    brief: { type: 'files', files },
    has_detail: false,
  });

  it('после текста реплики — «📎 имя, имя»', async () => {
    stubFetch();
    const w = mountWindow([sayEvent('владелец (next): смотри', [{ name: 'a.png', size: 1, kind: 'image' }, { name: 'b.pdf', size: 2, kind: 'pdf' }])]);
    await flushPromises();
    const row = w.get('[data-event-id="7"]');
    expect(row.text()).toBe('ты написал: смотри\n📎 a.png, b.pdf');
    w.unmount();
  });

  it('пустой текст с файлами — одна строка «📎 …»; say без brief — как раньше', () => {
    expect(stepView(sayEvent('владелец (next): ', [{ name: 'a.png', size: 1, kind: 'image' }])).text).toBe('ты написал:\n📎 a.png');
    const plain: SessionEvent = { ...sayEvent('владелец (next): привет', []), brief: null };
    expect(stepView(plain).text).toBe('ты написал: привет');
  });
});
