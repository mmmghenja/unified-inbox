// Ф20-3 C1 (`docs/architecture.md`): активность для статуса Slack — `POST /presence/active` раз в
// 60 с, только пока окно видимо и в фокусе И было действие за 5 минут; брошенная вкладка запросов не шлёт.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ACTIVITY_HEARTBEAT_MS, ACTIVITY_IDLE_MS, ActivityTracker } from '../src/store/activity';

describe('ActivityTracker', () => {
  let clock = 0;
  let active = true;
  let post: ReturnType<typeof vi.fn>;
  let tracker: ActivityTracker;

  const advance = (ms: number) => {
    clock += ms;
    vi.advanceTimersByTime(ms);
  };

  beforeEach(() => {
    vi.useFakeTimers();
    clock = 1_000_000;
    active = true;
    post = vi.fn(() => Promise.resolve({ ok: true }));
    tracker = new ActivityTracker({ post, isActive: () => active, now: () => clock });
    tracker.start();
  });

  afterEach(() => {
    tracker.stop();
    vi.useRealTimers();
  });

  it('без действий запросов нет, даже при видимом окне в фокусе', () => {
    advance(10 * 60_000);
    expect(post).not.toHaveBeenCalled();
  });

  it('первое действие — сразу запрос, дальше раз в 60 с, пока действия свежие (до 5 мин)', () => {
    window.dispatchEvent(new Event('keydown'));
    expect(post).toHaveBeenCalledTimes(1);
    advance(ACTIVITY_HEARTBEAT_MS);
    expect(post).toHaveBeenCalledTimes(2);
    window.dispatchEvent(new Event('pointerdown'));
    advance(ACTIVITY_HEARTBEAT_MS);
    expect(post).toHaveBeenCalledTimes(3);
  });

  it('брошенная вкладка: через 5 минут без действий запросы прекращаются', () => {
    window.dispatchEvent(new Event('keydown'));
    for (let i = 0; i < 4; i++) advance(ACTIVITY_HEARTBEAT_MS);
    const sentWhileFresh = post.mock.calls.length;
    expect(sentWhileFresh).toBeGreaterThanOrEqual(5);
    advance(ACTIVITY_IDLE_MS);
    advance(ACTIVITY_HEARTBEAT_MS);
    const afterIdle = post.mock.calls.length;
    advance(5 * ACTIVITY_HEARTBEAT_MS);
    expect(post.mock.calls.length).toBe(afterIdle);
  });

  it('окно не в фокусе или скрыто — запросов нет, даже при действиях', () => {
    active = false;
    window.dispatchEvent(new Event('mousemove'));
    advance(3 * ACTIVITY_HEARTBEAT_MS);
    expect(post).not.toHaveBeenCalled();
    active = true;
    advance(ACTIVITY_HEARTBEAT_MS);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('после stop() события не слушаются, таймер снят', () => {
    tracker.stop();
    window.dispatchEvent(new Event('keydown'));
    advance(3 * ACTIVITY_HEARTBEAT_MS);
    expect(post).not.toHaveBeenCalled();
  });

  it('ошибка сети глотается', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    post.mockImplementation(() => Promise.reject(new Error('net')));
    window.dispatchEvent(new Event('keydown'));
    await Promise.resolve();
    await Promise.resolve();
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });
});
