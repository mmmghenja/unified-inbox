// Ф8-1 A (`docs/architecture.md`), тесты T1–T5: синхронизация не застывает. Настоящий `Store` с
// замоканным `fetch` и фейковым `EventSource` — как в `14-store-lifecycle.spec.ts`.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Message, StatusResponse, SyncResponse } from '../src/api/types';
import { createStore, type Store } from '../src/store/store';
import type { EventSourceLike } from '../src/store/stream';

// T1: «ядовитое» сообщение — `applySync` бросает на любой пачке, где есть сообщение с этим id (эмуляция
// строки, на которой редьюсер падает). Остальное — настоящий редьюсер.
const POISON_ID = 666;
vi.mock('../src/store/applySync', async (importOriginal) => {
  const original = await importOriginal<typeof import('../src/store/applySync')>();
  return {
    ...original,
    applySync: (state: Parameters<typeof original.applySync>[0], batch: Parameters<typeof original.applySync>[1]) => {
      if (batch.messages.some((m) => m.id === POISON_ID)) throw new Error('ядовитая строка');
      original.applySync(state, batch);
    },
  };
});

class FakeEventSource implements EventSourceLike {
  readyState = 1;
  addEventListener(): void {}
  close(): void {}
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function baseMessage(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 1,
    ext_id: '1',
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'привет',
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
    sender: { id: 1, name: 'Коллега', handle: null },
    ...overrides,
  };
}

const STATUS: StatusResponse = {
  accounts: [],
  dead_letter: {},
  outbox_pending: 0,
  outbox_approval: 0,
  outbox_unknown: 0,
  max_seq: 500,
  now: 0,
};

const EMPTY_SYNC: SyncResponse = { messages: [], channels: [], max_seq: 500, more: false };

type SyncHandler = (since: string) => Promise<Response>;

/** Роутер `fetch`: `/sync` отдаётся текущим `syncHandler` (его можно менять по ходу теста). */
function makeRouter() {
  const syncCalls: string[] = [];
  const router = {
    syncCalls,
    syncHandler: (async () => jsonResponse(EMPTY_SYNC)) as SyncHandler,
    fetchMock: vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      switch (url.pathname) {
        case '/api/v1/auth/me':
          return jsonResponse({ token_id: 1, kind: 'ui', scopes: ['read'], project_ids: null, csrf: 'c' });
        case '/api/v1/status':
          return jsonResponse(STATUS);
        case '/api/v1/projects':
          return jsonResponse([]);
        case '/api/v1/sync': {
          const since = url.searchParams.get('since') ?? '';
          syncCalls.push(since);
          return router.syncHandler(since);
        }
        default:
          throw new Error(`нет обработчика для ${url.pathname}`);
      }
    }),
  };
  return router;
}

function fakeWindowWithoutCode() {
  return {
    location: { hash: '', pathname: '/', search: '' },
    history: { replaceState: () => {} },
  };
}

async function bootedStore(router: ReturnType<typeof makeRouter>): Promise<Store> {
  vi.stubGlobal('fetch', router.fetchMock);
  const store = createStore({ createEventSource: () => new FakeEventSource() });
  await store.bootstrap(fakeWindowWithoutCode());
  await vi.advanceTimersByTimeAsync(0);
  return store;
}

function stopBackground(store: Store): void {
  (store as unknown as { stopBackground(): void }).stopBackground();
}

let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  api.setCsrf(null);
  api.setUnauthorizedHandler(null);
  vi.useFakeTimers();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  errorSpy.mockRestore();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
});

describe('T1: одна ядовитая строка не останавливает ленту', () => {
  it('остальные сообщения пачки в сторе, lastSeq = max_seq, следующий /sync идёт с нового since', async () => {
    const router = makeRouter();
    const store = await bootedStore(router);
    expect(store.state.sync.lastSeq).toBe(500);

    router.syncHandler = async () =>
      jsonResponse({
        messages: [
          baseMessage({ id: 901, seq: 501, first_seq: 501, ext_id: '901' }),
          baseMessage({ id: POISON_ID, seq: 502, first_seq: 502, ext_id: '666' }),
          baseMessage({ id: 903, seq: 503, first_seq: 503, ext_id: '903' }),
        ],
        channels: [],
        max_seq: 503,
        more: false,
      } satisfies SyncResponse);
    await store.runSync();

    const ids = store.messagesOf(5).map((m) => m.id);
    expect(ids).toEqual([901, 903]);
    expect(store.state.sync.lastSeq).toBe(503);
    expect(store.state.syncError).toBeNull();
    expect(errorSpy.mock.calls.some((args) => String(args[0]).includes(String(POISON_ID)))).toBe(true);

    router.syncHandler = async () => jsonResponse({ ...EMPTY_SYNC, max_seq: 503 });
    await store.runSync();
    expect(router.syncCalls.at(-1)).toBe('503');
    stopBackground(store);
  });
});

// Ф8-3 D4: уведомлений браузера больше нет — бросающий `Notification` даже не конструируется.
describe('T2: сбой уведомления не мешает ленте', () => {
  it('Notification бросает в конструкторе — не вызывается вовсе, сообщение в сторе, lastSeq сдвинут', async () => {
    let constructed = 0;
    class ThrowingNotification {
      static permission = 'granted';
      static requestPermission = vi.fn();
      constructor() {
        constructed += 1;
        throw new Error('Notification сломан');
      }
    }
    vi.stubGlobal('Notification', ThrowingNotification);
    const router = makeRouter();
    const store = await bootedStore(router);
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });

    router.syncHandler = async () =>
      jsonResponse({
        messages: [baseMessage({ id: 910, seq: 501, first_seq: 501 })],
        channels: [],
        max_seq: 501,
        more: false,
      } satisfies SyncResponse);
    await store.runSync();

    expect(constructed).toBe(0); // Ф8-3 D4: уведомление не показывается вовсе
    expect(store.messagesOf(5).map((m) => m.id)).toEqual([910]);
    expect(store.state.sync.lastSeq).toBe(501);
    expect(store.state.syncError).toBeNull();
    stopBackground(store);
  });
});

describe('T3: /sync падает — syncError и повтор с бэкоффом', () => {
  it('сеть отказала -> syncError; через 2 с повтор; успех очищает syncError', async () => {
    const router = makeRouter();
    const store = await bootedStore(router);

    router.syncHandler = async () => {
      throw new TypeError('Failed to fetch');
    };
    await store.runSync();
    expect(store.state.syncError).not.toBeNull();
    expect(store.state.syncError!.message).toBe('network error');
    const callsAfterFail = router.syncCalls.length;

    await vi.advanceTimersByTimeAsync(1999);
    expect(router.syncCalls.length).toBe(callsAfterFail); // бэкофф ещё не истёк

    await vi.advanceTimersByTimeAsync(1);
    expect(router.syncCalls.length).toBe(callsAfterFail + 1); // первый повтор — через 2 с
    expect(store.state.syncError).not.toBeNull(); // всё ещё падает

    await vi.advanceTimersByTimeAsync(4999);
    expect(router.syncCalls.length).toBe(callsAfterFail + 1);
    router.syncHandler = async () => jsonResponse(EMPTY_SYNC);
    await vi.advanceTimersByTimeAsync(1);
    expect(router.syncCalls.length).toBe(callsAfterFail + 2); // второй повтор — через 5 с
    expect(store.state.syncError).toBeNull();
    stopBackground(store);
  });

  it('5xx — тоже syncError и повтор', async () => {
    const router = makeRouter();
    const store = await bootedStore(router);
    router.syncHandler = async () => jsonResponse({ detail: 'boom' }, 503);
    await store.runSync();
    expect(store.state.syncError).not.toBeNull();
    const before = router.syncCalls.length;
    router.syncHandler = async () => jsonResponse(EMPTY_SYNC);
    await vi.advanceTimersByTimeAsync(2000);
    expect(router.syncCalls.length).toBe(before + 1);
    expect(store.state.syncError).toBeNull();
    stopBackground(store);
  });

  it('401 — экран «сессия истекла», без syncError и без повторов', async () => {
    const router = makeRouter();
    const store = await bootedStore(router);
    router.syncHandler = async () => jsonResponse({ detail: 'unauthorized' }, 401);
    await store.runSync();
    expect(store.state.screen).toBe('session-expired');
    expect(store.state.syncError).toBeNull();
    const before = router.syncCalls.length;
    await vi.advanceTimersByTimeAsync(300_000);
    expect(router.syncCalls.length).toBe(before);
  });
});

describe('T4: повисший fetch не держит syncing навсегда', () => {
  it('через 20 с NetworkError, syncing освобождается, следующий runSync выполняется', async () => {
    const router = makeRouter();
    const store = await bootedStore(router);
    const before = router.syncCalls.length;

    router.syncHandler = () => new Promise<Response>(() => {}); // никогда не резолвится
    const hanging = store.runSync();
    await vi.advanceTimersByTimeAsync(0);
    expect(router.syncCalls.length).toBe(before + 1);

    // Пока висит — новый вызов только ставит «повторить после», сам запрос не уходит.
    void store.runSync();
    expect(router.syncCalls.length).toBe(before + 1);

    router.syncHandler = async () => jsonResponse(EMPTY_SYNC);
    await vi.advanceTimersByTimeAsync(api.REQUEST_TIMEOUT_MS);
    await hanging;
    expect(errorSpy).toHaveBeenCalled();
    // syncing освобождён: отложенный повтор уже прошёл, и новый явный runSync тоже выполняется.
    expect(router.syncCalls.length).toBeGreaterThanOrEqual(before + 2);
    const afterTimeout = router.syncCalls.length;
    await store.runSync();
    expect(router.syncCalls.length).toBe(afterTimeout + 1);
    expect(store.state.syncError).toBeNull();
    stopBackground(store);
  });

  it('клиент API сам: повисший fetch -> NetworkError ровно через REQUEST_TIMEOUT_MS', async () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})));
    const result = api.getStatus().then(
      () => 'resolved',
      (err: unknown) => err,
    );
    await vi.advanceTimersByTimeAsync(api.REQUEST_TIMEOUT_MS - 1);
    let settled = false;
    void result.then(() => {
      settled = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await result).toBeInstanceOf(api.NetworkError);
  });
});

describe('T5: страховочный /sync по таймеру, независимо от SSE', () => {
  it('без кадров SSE /sync раз в 30 с при видимой вкладке; после stopBackground — нет', async () => {
    const router = makeRouter();
    const store = await bootedStore(router);
    const start = router.syncCalls.length;

    await vi.advanceTimersByTimeAsync(29_999);
    expect(router.syncCalls.length).toBe(start);
    await vi.advanceTimersByTimeAsync(1);
    expect(router.syncCalls.length).toBe(start + 1);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(router.syncCalls.length).toBe(start + 2);

    stopBackground(store);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(router.syncCalls.length).toBe(start + 2);
  });

  it('скрытая вкладка — раз в 2 мин', async () => {
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    const router = makeRouter();
    const store = await bootedStore(router);
    const start = router.syncCalls.length;

    await vi.advanceTimersByTimeAsync(60_000);
    expect(router.syncCalls.length).toBe(start);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(router.syncCalls.length).toBe(start + 1);
    stopBackground(store);
  });
});
