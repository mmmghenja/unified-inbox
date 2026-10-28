// Ф41-3б (`docs/architecture.md`): текст по мере набора. Одно соединение `EventSource` на
// `/api/v1/sessions/live` на все раскрытые окна (закрывается, когда окон нет или вкладка скрыта, переподключение через
// 2 с), строка «пишет…» в конце ленты, замена окончательным `text` с тем же `ref`, снятие по `done` (через 5 с) и по
// состоянию сессии, размышления — свёрнутый блок «думает…», перерисовка не чаще 4 раз в секунду.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import * as api from '../src/api/client';
import type { HubSession, SessionEvent, SessionLive } from '../src/api/types';
import SessionWindow from '../src/components/SessionWindow.vue';
import { store } from '../src/store/instance';
import { createSessionsState, mergeSessionEvents, upsertSession } from '../src/store/sessions';

const NOW = new Date(2026, 10, 8, 15, 0, 0).getTime();

class FakeES {
  static all: FakeES[] = [];
  readonly listeners = new Map<string, ((ev: { data: string }) => void)[]>();
  closed = false;
  constructor(public readonly url: string) {
    FakeES.all.push(this);
  }
  addEventListener(type: string, fn: (ev: { data: string }) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  close(): void {
    this.closed = true;
  }
  emit(type: string, data: unknown = ''): void {
    for (const fn of this.listeners.get(type) ?? []) fn({ data: typeof data === 'string' ? data : JSON.stringify(data) });
  }
}

function open(): FakeES[] {
  return FakeES.all.filter((es) => !es.closed);
}

function frame(overrides: Partial<SessionLive> & { session_id?: number } = {}) {
  return { session_id: 5, block: 'msg_1:0', kind: 'text', text: 'пишу', cut: false, done: false, ...overrides };
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

function textEvent(overrides: Partial<SessionEvent>): SessionEvent {
  return { id: 30, ts: NOW, kind: 'text', tool: null, summary: 'готово', is_error: false, ref: 'msg_1:0', ...overrides };
}

function mountWindow(overrides: Partial<HubSession> = {}, events: SessionEvent[] = []) {
  const s = session(overrides);
  store.state.sessions.items[s.id] = s;
  store.state.sessions.events[s.id] = events;
  return mount(SessionWindow, { props: { session: s } });
}

function setVisibility(value: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => value });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  FakeES.all = [];
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sessions = createSessionsState();
  vi.stubGlobal('EventSource', FakeES);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      const body = /\/events$/.test(p) ? [] : /^\/api\/v1\/sessions\/\d+$/.test(p) ? { session: session(), requests: [], says: [] } : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
    }),
  );
});

afterEach(() => {
  setVisibility('visible');
  delete (document as unknown as Record<string, unknown>).visibilityState;
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-3б: одно соединение на все окна', () => {
  it('одно окно — один EventSource на /sessions/live; второе окно соединения не добавляет; закрывается, когда окон нет', async () => {
    const a = mountWindow({ id: 5 });
    await flushPromises();
    expect(FakeES.all).toHaveLength(1);
    expect(FakeES.all[0]!.url).toBe('/api/v1/sessions/live');
    const b = mountWindow({ id: 6 });
    await flushPromises();
    expect(FakeES.all).toHaveLength(1);
    a.unmount();
    expect(open()).toHaveLength(1);
    b.unmount();
    expect(open()).toHaveLength(0);
  });

  it('вкладка скрыта — соединения нет, видима снова — открывается заново', async () => {
    const w = mountWindow();
    await flushPromises();
    expect(open()).toHaveLength(1);
    setVisibility('hidden');
    expect(open()).toHaveLength(0);
    setVisibility('visible');
    expect(open()).toHaveLength(1);
    expect(FakeES.all).toHaveLength(2);
    w.unmount();
    expect(open()).toHaveLength(0);
  });

  it('обрыв — переподключение через 2 с, не раньше', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    const w = mountWindow();
    await nextTick();
    expect(FakeES.all).toHaveLength(1);
    FakeES.all[0]!.emit('error');
    expect(open()).toHaveLength(0);
    vi.advanceTimersByTime(1999);
    expect(FakeES.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeES.all).toHaveLength(2);
    expect(open()).toHaveLength(1);
    w.unmount();
    expect(open()).toHaveLength(0);
  });
});

describe('Ф41-3б: строка «пишет…»', () => {
  it('кадр live рисует «пишет…» с текстом через ModelText (markdown), без утечки в историю', async () => {
    const w = mountWindow();
    await flushPromises();
    expect(w.find('.session-live').exists()).toBe(false);
    FakeES.all[0]!.emit('live', frame({ text: 'делаю **важное**' }));
    await nextTick();
    const line = w.get('.session-live');
    expect(line.text()).toContain('пишет…');
    expect(line.get('strong').text()).toBe('важное');
    expect(store.state.sessions.events[5]).toEqual([]);
    w.unmount();
  });

  it('текст блока обновляется следующим кадром (весь текст, не добавка)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    const w = mountWindow();
    await nextTick();
    FakeES.all[0]!.emit('live', frame({ text: 'раз' }));
    await nextTick();
    expect(w.get('.session-live').text()).toContain('раз');
    vi.advanceTimersByTime(300);
    FakeES.all[0]!.emit('live', frame({ text: 'раз два' }));
    await nextTick();
    expect(w.get('.session-live .md').text()).toBe('раз два');
    w.unmount();
  });

  it('перерисовка текста не чаще 4 раз в секунду', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    const w = mountWindow();
    await nextTick();
    const es = FakeES.all[0]!;
    es.emit('live', frame({ text: 'a' }));
    await nextTick();
    vi.advanceTimersByTime(50);
    es.emit('live', frame({ text: 'ab' }));
    vi.advanceTimersByTime(50);
    es.emit('live', frame({ text: 'abc' }));
    await nextTick();
    expect(w.get('.session-live .md').text()).toBe('a'); // ещё в пределах 250 мс
    vi.advanceTimersByTime(200);
    await nextTick();
    expect(w.get('.session-live .md').text()).toBe('abc'); // последнее состояние, промежуточное пропущено
    w.unmount();
  });

  it('размышления — свёрнутый блок «думает…», раскрывается по клику; cut — «начало скрыто»', async () => {
    const w = mountWindow();
    await flushPromises();
    const es = FakeES.all[0]!;
    es.emit('live', frame({ kind: 'thinking', text: 'взвешиваю варианты' }));
    await nextTick();
    expect(w.get('.session-live').text()).toContain('думает…');
    expect(w.get('.session-live').text()).not.toContain('взвешиваю');
    await w.get('.session-live-think').trigger('click');
    expect(w.get('.session-live').text()).toContain('взвешиваю варианты');
    es.emit('live', frame({ block: 'msg_1:1', text: 'хвост', cut: true }));
    await nextTick();
    expect(w.get('.session-live').text()).toContain('начало скрыто');
    w.unmount();
  });

  it('окончательный text с тем же ref заменяет строку; опоздавший кадр её не воскрешает', async () => {
    const w = mountWindow();
    await flushPromises();
    const es = FakeES.all[0]!;
    es.emit('live', frame({ text: 'почти' }));
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(true);
    mergeSessionEvents(store.state.sessions, 5, [textEvent({ id: 30, ref: 'msg_1:0', summary: 'готово целиком' })]);
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(false);
    expect(w.get('[data-event-id="30"]').text()).toContain('готово целиком');
    es.emit('live', frame({ text: 'почти', done: false }));
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(false);
    // другой блок — новая строка
    es.emit('live', frame({ block: 'msg_1:2', text: 'следующий' }));
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(true);
    w.unmount();
  });

  it('text с другим ref строку не снимает', async () => {
    const w = mountWindow();
    await flushPromises();
    FakeES.all[0]!.emit('live', frame({ block: 'msg_2:0' }));
    mergeSessionEvents(store.state.sessions, 5, [textEvent({ ref: 'msg_1:0' })]);
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(true);
    w.unmount();
  });

  it('через 5 с после done строка снимается (раньше — держится)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    const w = mountWindow();
    await nextTick();
    FakeES.all[0]!.emit('live', frame({ done: true }));
    await nextTick();
    vi.advanceTimersByTime(4999);
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(true);
    vi.advanceTimersByTime(1);
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(false);
    w.unmount();
  });

  it('состояние сессии не running — строки нет; запись уходит из стора', async () => {
    const w = mountWindow({ state: 'idle' });
    await flushPromises();
    FakeES.all[0]!.emit('live', frame());
    await nextTick();
    expect(w.find('.session-live').exists()).toBe(false); // не в ходу — не показываем
    upsertSession(store.state.sessions, session({ state: 'running', seq: 11 }));
    expect(store.state.sessions.live[5]).toBeDefined();
    upsertSession(store.state.sessions, session({ state: 'idle', seq: 12 }));
    expect(store.state.sessions.live[5]).toBeUndefined();
    w.unmount();
  });

  it('законченный блок переживает конец хода: держится до события text с тем же ref', async () => {
    const w = mountWindow();
    await flushPromises();
    FakeES.all[0]!.emit('live', frame({ done: true }));
    await nextTick();
    upsertSession(store.state.sessions, session({ state: 'idle', seq: 12 }));
    await w.setProps({ session: store.state.sessions.items[5]! });
    await nextTick();
    expect(store.state.sessions.live[5]).toBeDefined();
    expect(w.find('.session-live').exists()).toBe(true); // текст не пропадает до догрузки ленты
    w.unmount();
  });

  it('кадр с мусорным телом игнорируется', async () => {
    const w = mountWindow();
    await flushPromises();
    FakeES.all[0]!.emit('live', 'не json');
    FakeES.all[0]!.emit('live', { session_id: 'x' });
    await nextTick();
    expect(store.state.sessions.live).toEqual({});
    w.unmount();
  });
});
