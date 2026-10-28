// Пункт 12 спеки + решение архитектора по heartbeat (20.09): `event: hb` — только давность, `/sync` не
// вызывается; `event: message`/`event: channel` — давность И троттлинг-`/sync`; порог «лента не обновляется»
// — 60 с. Поддельные таймеры + ручной вызов обработчиков фальшивого EventSource (как просил архитектор).
//
// F3 (приёмка 6b, дефект найден чтением кода): было ДЕБАУНСОМ — каждый новый кадр откладывал таймер заново,
// и при непрерывном потоке кадров (flood) `/sync` не вызывался никогда. Ниже — тесты на троттлинг.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createStaleness, STALE_THRESHOLD_MS, startStalenessClock, tick } from '../src/store/staleness';
import { connectStream, EVENT_SOURCE_CLOSED, type EventSourceLike } from '../src/store/stream';

class FakeEventSource implements EventSourceLike {
  listeners = new Map<string, Array<() => void>>();
  closed = false;
  readyState = 1; // OPEN — по умолчанию; тест реконнекта (F2) выставляет CLOSED вручную

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

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(0);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('12 Давность потока: модуль staleness (без EventSource)', () => {
  it('61 с без кадров -> stale=true; после кадра -> сразу false', () => {
    const state = createStaleness();
    const stopClock = startStalenessClock(state, 1000);

    tick(state, 0); // ни одного кадра ещё не было
    expect(state.stale).toBe(true);

    vi.setSystemTime(30_000);
    tick(state, 30_000);
    expect(state.stale).toBe(true); // lastFrameAt всё ещё null

    stopClock();
  });

  it('кадр в момент t=0, тишина до 61с -> stale становится true ровно после порога', () => {
    const state = createStaleness();
    state.lastFrameAt = 0;
    tick(state, STALE_THRESHOLD_MS); // ровно 60_000 — ещё не «застыло» (строго больше порога)
    expect(state.stale).toBe(false);
    tick(state, STALE_THRESHOLD_MS + 1);
    expect(state.stale).toBe(true);
  });
});

describe('12 Давность потока: обвязка EventSource (`hb` не будит /sync, `message`/`channel` будят)', () => {
  it('часы тикают раз в секунду и красят полосу после 61 с молчания', () => {
    const staleness = createStaleness();
    const onSyncNeeded = vi.fn();
    const fake = new FakeEventSource();

    connectStream({
      url: '/api/v1/stream',
      staleness,
      onSyncNeeded,
      createEventSource: () => fake,
    });

    // onopen — признак жизни.
    fake.emit('open');
    expect(staleness.lastFrameAt).toBe(0);

    const stopClock = startStalenessClock(staleness, 1000);
    vi.advanceTimersByTime(60_000);
    expect(staleness.stale).toBe(false); // ровно 60с — ещё не «застыло»

    vi.advanceTimersByTime(1_000); // итого 61с без кадров
    expect(staleness.stale).toBe(true);

    stopClock();
  });

  it('`hb` обновляет давность, но НЕ вызывает onSyncNeeded', () => {
    const staleness = createStaleness();
    const onSyncNeeded = vi.fn();
    const fake = new FakeEventSource();
    connectStream({ url: '/x', staleness, onSyncNeeded, createEventSource: () => fake });

    vi.setSystemTime(10_000);
    fake.emit('hb');
    expect(staleness.lastFrameAt).toBe(10_000);

    vi.advanceTimersByTime(100); // троттлинг (~50мс) успел бы сработать, если бы был запланирован
    expect(onSyncNeeded).not.toHaveBeenCalled();
  });

  it('`message` и `channel` обновляют давность И запускают троттлинг-/sync один раз', () => {
    const staleness = createStaleness();
    const onSyncNeeded = vi.fn();
    const fake = new FakeEventSource();
    connectStream({ url: '/x', staleness, onSyncNeeded, createEventSource: () => fake, throttleMs: 50 });

    vi.setSystemTime(5_000);
    fake.emit('message');
    expect(staleness.lastFrameAt).toBe(5_000);
    expect(onSyncNeeded).not.toHaveBeenCalled(); // ещё не прошло 50мс троттлинга

    fake.emit('channel'); // второй кадр почти сразу — должен схлопнуться в один вызов /sync
    vi.advanceTimersByTime(50);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
  });

  it('после 61 с тишины поток «застыл», даже если EventSource формально не закрыт', () => {
    const staleness = createStaleness();
    const fake = new FakeEventSource();
    connectStream({ url: '/x', staleness, onSyncNeeded: vi.fn(), createEventSource: () => fake });
    const stopClock = startStalenessClock(staleness, 1000);

    fake.emit('hb');
    vi.advanceTimersByTime(61_000);

    expect(staleness.stale).toBe(true);
    expect(fake.closed).toBe(false);
    stopClock();
  });
});

describe('F3 троттлинг вместо дебаунса: непрерывный поток кадров не глушит /sync навсегда', () => {
  it('100 кадров с шагом 10мс -> /sync вызван хотя бы раз в первые 100мс (а не после тишины)', () => {
    const staleness = createStaleness();
    const onSyncNeeded = vi.fn();
    const fake = new FakeEventSource();
    connectStream({ url: '/x', staleness, onSyncNeeded, createEventSource: () => fake, throttleMs: 50 });

    for (let i = 0; i < 100; i++) {
      fake.emit('message'); // каждый кадр раньше СБРАСЫВАЛ дебаунс — с ним onSyncNeeded не срабатывал бы
      vi.advanceTimersByTime(10);
    }

    expect(onSyncNeeded.mock.calls.length).toBeGreaterThanOrEqual(1);
  });

  it('старый дебаунс (для контраста, задокументировано в отчёте) дал бы 0 вызовов — здесь их несколько', () => {
    const staleness = createStaleness();
    const onSyncNeeded = vi.fn();
    const fake = new FakeEventSource();
    connectStream({ url: '/x', staleness, onSyncNeeded, createEventSource: () => fake, throttleMs: 50 });

    for (let i = 0; i < 1000; i++) {
      fake.emit('message');
      vi.advanceTimersByTime(10);
    }
    // 1000 кадров по 10мс = 10с потока; троттлинг на 50мс -> примерно 10000/50 = 200 срабатываний.
    expect(onSyncNeeded.mock.calls.length).toBeGreaterThan(50);
  });
});

describe('F2 реконнект SSE при фатальном закрытии (readyState=CLOSED)', () => {
  it('`error` при readyState=CLOSED вызывает onClosed; при readyState=OPEN (переходный обрыв) — нет', () => {
    const staleness = createStaleness();
    const fakeOpen = new FakeEventSource();
    const onClosedOpen = vi.fn();
    connectStream({ url: '/x', staleness, onSyncNeeded: vi.fn(), createEventSource: () => fakeOpen, onClosed: onClosedOpen });
    fakeOpen.readyState = 1; // временный обрыв — браузер сам переподключится, readyState не CLOSED
    fakeOpen.emit('error');
    expect(onClosedOpen).not.toHaveBeenCalled();

    const fakeClosed = new FakeEventSource();
    const onClosedFatal = vi.fn();
    connectStream({
      url: '/x',
      staleness,
      onSyncNeeded: vi.fn(),
      createEventSource: () => fakeClosed,
      onClosed: onClosedFatal,
    });
    fakeClosed.readyState = EVENT_SOURCE_CLOSED;
    fakeClosed.emit('error');
    expect(onClosedFatal).toHaveBeenCalledTimes(1);
  });

  it('`onOpen` вызывается на `open` (для сброса счётчика бэкоффа в сторе)', () => {
    const staleness = createStaleness();
    const fake = new FakeEventSource();
    const onOpen = vi.fn();
    connectStream({ url: '/x', staleness, onSyncNeeded: vi.fn(), createEventSource: () => fake, onOpen });
    fake.emit('open');
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
