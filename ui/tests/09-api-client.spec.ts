// Пункт 9 спеки: API-клиент — CSRF/X-Hub-Client на не-GET, повтор `send` при сетевой ошибке с тем же
// idempotency_key, 401 -> обработчик экрана входа.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface FetchCallInit {
  headers: Record<string, string>;
  body?: string;
  credentials?: string;
  method?: string;
}

beforeEach(() => {
  api.setCsrf(null);
  api.setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('09 API-клиент: заголовки', () => {
  it('GET: X-Hub-Client есть, X-Hub-CSRF нет, credentials same-origin', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await api.listProjects();

    const init = fetchMock.mock.calls[0]![1] as FetchCallInit;
    expect(init.headers['X-Hub-Client']).toBe('hub-ui');
    expect(init.headers['X-Hub-CSRF']).toBeUndefined();
    expect(init.credentials).toBe('same-origin');
  });

  it('не-GET: X-Hub-CSRF из текущего csrf, X-Hub-Client тоже', async () => {
    api.setCsrf('csrf-value-123');
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ outbox_id: 1, message_id: 2, status: 'queued', created: true }));
    vi.stubGlobal('fetch', fetchMock);

    await api.sendMessage({ channelId: 1, text: 'привет', idempotencyKey: 'k-1' });

    const init = fetchMock.mock.calls[0]![1] as FetchCallInit;
    expect(init.headers['X-Hub-CSRF']).toBe('csrf-value-123');
    expect(init.headers['X-Hub-Client']).toBe('hub-ui');
    expect(init.method).toBe('POST');
  });
});

describe('09 API-клиент: send — повтор при сетевой ошибке с тем же idempotency_key', () => {
  it('первая попытка — сетевая ошибка, вторая — успех, ключ идентичен', async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValueOnce(
        jsonResponse({ outbox_id: 1, message_id: 2, status: 'queued', created: true }),
      );
    vi.stubGlobal('fetch', fetchMock);

    const result = await api.sendMessage({ channelId: 7, text: 'привет' });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const body1 = JSON.parse((fetchMock.mock.calls[0]![1] as FetchCallInit).body ?? '{}');
    const body2 = JSON.parse((fetchMock.mock.calls[1]![1] as FetchCallInit).body ?? '{}');
    expect(body1.idempotency_key).toBeTruthy();
    expect(body1.idempotency_key).toBe(body2.idempotency_key);
    expect(result.status).toBe('queued');
  });

  it('HTTP-ошибка (не сетевая) не повторяется тем же ключом', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'idempotency conflict' }, 409));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.sendMessage({ channelId: 7, text: 'привет' })).rejects.toBeInstanceOf(api.ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('09 API-клиент: 401 -> обработчик «сессия истекла»', () => {
  it('вызывается на любом запросе, не только на входе', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.listProjects()).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

// Пункт 9 спеки 2e: новые ручки тредов — пути (в т.ч. кодирование `thread_ext_id` вида ts Slack, с точкой),
// CSRF-заголовок на POST, `thread`/`reply_to` в теле отправки (уже были у `sendMessage` — проверяем явно).
describe('09 API-клиент: ручки тредов (спека 2e)', () => {
  it('listThreadMessages: путь с закодированным thread_ext_id и параметрами before/limit, GET без CSRF', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ messages: [], next_before: null, parent: null }));
    vi.stubGlobal('fetch', fetchMock);

    await api.listThreadMessages(7, '1700000000.000100', { before: '1,2,3', limit: 20 });

    const [url, init] = fetchMock.mock.calls[0]! as [string, FetchCallInit];
    expect(url).toBe('/api/v1/channels/7/threads/1700000000.000100/messages?before=1%2C2%2C3&limit=20');
    expect(init.headers['X-Hub-CSRF']).toBeUndefined();
  });

  it('listChannelThreads: unread=1 только когда запрошено, иначе параметр не уходит вовсе', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await api.listChannelThreads(7, { unread: true });
    expect((fetchMock.mock.calls[0]![0] as string)).toBe('/api/v1/channels/7/threads?unread=1');

    fetchMock.mockClear();
    await api.listChannelThreads(7);
    expect((fetchMock.mock.calls[0]![0] as string)).toBe('/api/v1/channels/7/threads');
  });

  it('markThreadRead: POST на путь с закодированным thread_ext_id, CSRF-заголовок и тело', async () => {
    api.setCsrf('csrf-xyz');
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ changed: true }));
    vi.stubGlobal('fetch', fetchMock);

    await api.markThreadRead(7, '1700000000.000100', 42);

    const [url, init] = fetchMock.mock.calls[0]! as [string, FetchCallInit];
    expect(url).toBe('/api/v1/channels/7/threads/1700000000.000100/read');
    expect(init.method).toBe('POST');
    expect(init.headers['X-Hub-CSRF']).toBe('csrf-xyz');
    expect(JSON.parse(init.body ?? '{}')).toEqual({ up_to_message_id: 42 });
  });

  it('sendMessage: thread/reply_to уходят в теле POST (или null, когда не заданы)', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ outbox_id: 1, message_id: 2, status: 'queued', created: true }));
    vi.stubGlobal('fetch', fetchMock);

    await api.sendMessage({ channelId: 1, text: 'в тред', thread: '1700000000.000100' });
    const body1 = JSON.parse((fetchMock.mock.calls[0]![1] as FetchCallInit).body ?? '{}');
    expect(body1.thread).toBe('1700000000.000100');
    expect(body1.reply_to).toBeNull();

    fetchMock.mockClear();
    await api.sendMessage({ channelId: 1, text: 'ответ', replyTo: '999' });
    const body2 = JSON.parse((fetchMock.mock.calls[0]![1] as FetchCallInit).body ?? '{}');
    expect(body2.reply_to).toBe('999');
    expect(body2.thread).toBeNull();
  });
});
