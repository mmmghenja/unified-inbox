// F1, F2, F9 (приёмка 6b, дефекты найдены архитектором чтением кода). Гоняем настоящий `Store` (не только
// его подмодули) с замоканным `fetch` и инъекцией фейкового `EventSource` — так виден порядок реальных
// шагов `afterLogin()`, а не только поведение отдельно взятых функций.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Message, StatusResponse, SyncResponse } from '../src/api/types';
import { createStore } from '../src/store/store';
import { EVENT_SOURCE_CLOSED, type EventSourceLike } from '../src/store/stream';

/** Дать отработать фоновым fire-and-forget промисам стора (F4 `scheduleProjectsRefresh`, начальный
 * `void this.runSync()` из `afterLogin()`) ПОКА мок `fetch` ещё активен — иначе они улетают в реальный
 * `fetch` уже после `vi.unstubAllGlobals()` следующего теста и падают необработанным rejection. */
async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

class FakeEventSource implements EventSourceLike {
  listeners = new Map<string, Array<() => void>>();
  readyState = 1;
  closed = false;

  addEventListener(type: string, listener: () => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  emit(type: string): void {
    for (const listener of this.listeners.get(type) ?? []) listener();
  }

  close(): void {
    this.closed = true;
  }
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

interface RouterOptions {
  syncResponses?: SyncResponse[]; // по одному на каждый вызов GET /sync, последний переиспользуется
}

function makeRouter(opts: RouterOptions = {}) {
  let syncCallIndex = 0;
  const syncCalls: string[] = []; // значения `since` каждого вызова, в порядке вызова
  const calls: string[] = []; // pathname каждого вызова, в порядке вызова

  const fetchMock = vi.fn(async (input: string) => {
    const url = new URL(input, 'http://localhost');
    calls.push(url.pathname);
    switch (url.pathname) {
      case '/api/v1/auth/me':
        return jsonResponse({ token_id: 1, kind: 'ui', scopes: ['read', 'send', 'admin'], project_ids: null, csrf: 'c' });
      case '/api/v1/status':
        return jsonResponse(STATUS);
      case '/api/v1/projects':
        return jsonResponse([]);
      case '/api/v1/sync': {
        syncCalls.push(url.searchParams.get('since') ?? '');
        const responses = opts.syncResponses ?? [];
        const body = responses[Math.min(syncCallIndex, responses.length - 1)] ?? {
          messages: [],
          channels: [],
          max_seq: STATUS.max_seq,
          more: false,
        };
        syncCallIndex += 1;
        return jsonResponse(body);
      }
      default:
        throw new Error(`нет обработчика для ${url.pathname}`);
    }
  });

  return { fetchMock, syncCalls, calls };
}

function fakeWindowWithoutCode() {
  return {
    location: { hash: '', pathname: '/', search: '' },
    history: { replaceState: () => {} },
  };
}

beforeEach(() => {
  api.setCsrf(null);
  api.setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('F1: lastSeq берётся из GET /status ДО первого /sync, а не с нуля', () => {
  it('первый вызов /sync уходит с since=max_seq статуса, а не since=0', async () => {
    const { fetchMock, syncCalls } = makeRouter();
    vi.stubGlobal('fetch', fetchMock);
    let createdEventSources = 0;
    const store = createStore({
      createEventSource: () => {
        createdEventSources += 1;
        return new FakeEventSource();
      },
    });

    await store.bootstrap(fakeWindowWithoutCode());
    await flush();

    expect(store.state.sync.lastSeq).toBe(500);
    expect(syncCalls.length).toBeGreaterThanOrEqual(1);
    expect(syncCalls[0]).toBe('500'); // НЕ '0' — это и был дефект F1
    expect(createdEventSources).toBe(1); // поток подключился (не только REST)
  });
});

describe('F2: реконнект SSE после фатального закрытия', () => {
  it('readyState=CLOSED + error -> проверка сессии -> переподключение через бэкофф -> новый /sync', async () => {
    vi.useFakeTimers();
    const { fetchMock } = makeRouter();
    vi.stubGlobal('fetch', fetchMock);

    const sources: FakeEventSource[] = [];
    const store = createStore({
      createEventSource: () => {
        const es = new FakeEventSource();
        sources.push(es);
        return es;
      },
    });

    await store.bootstrap(fakeWindowWithoutCode());
    expect(sources).toHaveLength(1);

    sources[0]!.readyState = EVENT_SOURCE_CLOSED;
    sources[0]!.emit('error');

    await vi.advanceTimersByTimeAsync(0); // дать отработать await api.authMe() внутри handleStreamClosed
    expect(sources).toHaveLength(1); // ещё не переподключились — ждём бэкофф (1с)

    await vi.advanceTimersByTimeAsync(1000);
    expect(sources).toHaveLength(2); // переподключились новым EventSource

    // Новый EventSource не несёт Last-Event-ID — дыру обязан закрыть явный /sync.
    const syncPaths = fetchMock.mock.calls.filter(([u]) => new URL(u as string, 'http://localhost').pathname === '/api/v1/sync');
    expect(syncPaths.length).toBeGreaterThanOrEqual(2); // из bootstrap + после реконнекта
  });

  it('если сессия истекла (authMe -> 401) — реконнект НЕ запускается, экран уже переключён обработчиком', async () => {
    vi.useFakeTimers();
    let meCallCount = 0;
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname === '/api/v1/auth/me') {
        meCallCount += 1;
        if (meCallCount === 1) {
          return jsonResponse({ token_id: 1, kind: 'ui', scopes: ['read'], project_ids: null, csrf: 'c' });
        }
        return jsonResponse({ detail: 'unauthorized' }, 401);
      }
      if (url.pathname === '/api/v1/status') return jsonResponse(STATUS);
      if (url.pathname === '/api/v1/projects') return jsonResponse([]);
      if (url.pathname === '/api/v1/sync') {
        return jsonResponse({ messages: [], channels: [], max_seq: STATUS.max_seq, more: false });
      }
      throw new Error(`нет обработчика для ${url.pathname}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const sources: FakeEventSource[] = [];
    const store = createStore({
      createEventSource: () => {
        const es = new FakeEventSource();
        sources.push(es);
        return es;
      },
    });

    await store.bootstrap(fakeWindowWithoutCode());
    expect(sources).toHaveLength(1);

    sources[0]!.readyState = EVENT_SOURCE_CLOSED;
    sources[0]!.emit('error');
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(15_000); // с запасом — даже до потолка бэкоффа

    expect(sources).toHaveLength(1); // ни одного реконнекта — сессии больше нет
    expect(store.state.screen).toBe('session-expired');
  });

  it('core перезапускается (authMe -> сетевая ошибка) — реконнект ВСЁ РАВНО запускается, экран не меняется', async () => {
    vi.useFakeTimers();
    let meCallCount = 0;
    const fetchMock = vi.fn(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname === '/api/v1/auth/me') {
        meCallCount += 1;
        if (meCallCount === 1) {
          return jsonResponse({ token_id: 1, kind: 'ui', scopes: ['read'], project_ids: null, csrf: 'c' });
        }
        throw new TypeError('Failed to fetch'); // так fetch сообщает об отказе соединения
      }
      if (url.pathname === '/api/v1/status') return jsonResponse(STATUS);
      if (url.pathname === '/api/v1/projects') return jsonResponse([]);
      if (url.pathname === '/api/v1/sync') {
        return jsonResponse({ messages: [], channels: [], max_seq: STATUS.max_seq, more: false });
      }
      throw new Error(`нет обработчика для ${url.pathname}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    const sources: FakeEventSource[] = [];
    const store = createStore({
      createEventSource: () => {
        const es = new FakeEventSource();
        sources.push(es);
        return es;
      },
    });

    await store.bootstrap(fakeWindowWithoutCode());
    sources[0]!.readyState = EVENT_SOURCE_CLOSED;
    sources[0]!.emit('error');
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(1000);

    expect(sources).toHaveLength(2);
    expect(store.state.screen).toBe('app');
  });
});

// Ф8-3 D4/T19 (было F9 — «уведомление только для НОВОГО сообщения»): браузерных уведомлений UI больше нет —
// их даёт разборщик. `Notification` не вызывается ни для нового сообщения в скрытой вкладке, ни для правки.
describe('Ф8-3 T19: уведомлений браузера больше нет', () => {
  function setupNotificationEnv(): { instances: Array<{ title: string; body?: string }> } {
    const instances: Array<{ title: string; body?: string }> = [];
    class NotificationStub {
      static permission = 'granted';
      static requestPermission = vi.fn();
      constructor(title: string, opts?: NotificationOptions) {
        instances.push({ title, body: opts?.body });
      }
    }
    vi.stubGlobal('Notification', NotificationStub);
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    return { instances };
  }

  it('новое сообщение в скрытой вкладке (first_seq > lastSeq) и правка старого -> Notification не вызывается', async () => {
    const { instances } = setupNotificationEnv();
    const { fetchMock } = makeRouter({
      syncResponses: [
        // ответ на самый первый /sync внутри afterLogin — пусто, lastSeq остаётся 500
        { messages: [], channels: [], max_seq: 500, more: false },
      ],
    });
    vi.stubGlobal('fetch', fetchMock);

    const store = createStore({ createEventSource: () => new FakeEventSource() });
    await store.bootstrap(fakeWindowWithoutCode());
    await flush();
    expect(store.state.sync.lastSeq).toBe(500);

    // Подменяем ответ /sync на «новое сообщение» и вызываем runSync руками (эмулируем кадр SSE).
    fetchMock.mockImplementation(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname === '/api/v1/sync') {
        return jsonResponse({
          messages: [baseMessage({ id: 900, seq: 501, first_seq: 501 })],
          channels: [],
          max_seq: 501,
          more: false,
        } satisfies SyncResponse);
      }
      if (url.pathname === '/api/v1/status') return jsonResponse(STATUS);
      if (url.pathname === '/api/v1/projects') return jsonResponse([]);
      throw new Error(`нет обработчика для ${url.pathname}`);
    });
    await store.runSync();
    await flush();
    expect(instances).toHaveLength(0); // новое сообщение — уведомления нет (Ф8-3 D4)
    expect(store.messagesOf(5).map((m) => m.id)).toEqual([900]); // а в ленту оно попало

    // Теперь "правка" старого сообщения: seq растёт (501 -> 502), но first_seq остаётся МЕНЬШЕ lastSeq (501).
    fetchMock.mockImplementation(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname === '/api/v1/sync') {
        return jsonResponse({
          messages: [baseMessage({ id: 900, seq: 502, first_seq: 501, text: 'исправлено' })],
          channels: [],
          max_seq: 502,
          more: false,
        } satisfies SyncResponse);
      }
      if (url.pathname === '/api/v1/status') return jsonResponse(STATUS);
      if (url.pathname === '/api/v1/projects') return jsonResponse([]);
      throw new Error(`нет обработчика для ${url.pathname}`);
    });
    await store.runSync();
    await flush();
    expect(instances).toHaveLength(0); // правка старого сообщения — тоже нет
  });

  it('kind === "event" (служебное сообщение) не уведомляет, даже если первое появление', async () => {
    const { instances } = setupNotificationEnv();
    const { fetchMock } = makeRouter({ syncResponses: [{ messages: [], channels: [], max_seq: 500, more: false }] });
    vi.stubGlobal('fetch', fetchMock);
    const store = createStore({ createEventSource: () => new FakeEventSource() });
    await store.bootstrap(fakeWindowWithoutCode());
    await flush();

    fetchMock.mockImplementation(async (input: string) => {
      const url = new URL(input, 'http://localhost');
      if (url.pathname === '/api/v1/sync') {
        return jsonResponse({
          messages: [baseMessage({ id: 901, seq: 501, first_seq: 501, kind: 'event', text: null })],
          channels: [],
          max_seq: 501,
          more: false,
        } satisfies SyncResponse);
      }
      if (url.pathname === '/api/v1/status') return jsonResponse(STATUS);
      if (url.pathname === '/api/v1/projects') return jsonResponse([]);
      throw new Error(`нет обработчика для ${url.pathname}`);
    });
    await store.runSync();
    await flush();
    expect(instances).toHaveLength(0);
  });
});

describe('F10: forceSyncListener требует ВИДИМОСТЬ И онлайн (было "или" — почти всегда true)', () => {
  afterEach(() => {
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    Object.defineProperty(navigator, 'onLine', { value: true, configurable: true });
  });

  it('online, но вкладка скрыта -> НЕ синхронизируется', async () => {
    const { fetchMock, calls } = makeRouter();
    vi.stubGlobal('fetch', fetchMock);
    const store = createStore({ createEventSource: () => new FakeEventSource() });
    await store.bootstrap(fakeWindowWithoutCode());
    await flush();
    const before = calls.filter((p) => p === '/api/v1/sync').length;

    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
    Object.defineProperty(navigator, 'onLine', { value: true, configurable: true });
    window.dispatchEvent(new Event('online'));
    await flush();

    expect(calls.filter((p) => p === '/api/v1/sync').length).toBe(before);
  });

  it('видима И online -> синхронизируется по visibilitychange (на document, не на window)', async () => {
    const { fetchMock, calls } = makeRouter();
    vi.stubGlobal('fetch', fetchMock);
    const store = createStore({ createEventSource: () => new FakeEventSource() });
    await store.bootstrap(fakeWindowWithoutCode());
    await flush();
    const before = calls.filter((p) => p === '/api/v1/sync').length;

    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    Object.defineProperty(navigator, 'onLine', { value: true, configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
    await flush();

    expect(calls.filter((p) => p === '/api/v1/sync').length).toBeGreaterThan(before);
  });
});
