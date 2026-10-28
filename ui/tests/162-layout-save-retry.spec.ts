// 21.10: «×» у линии «не в сети» терялся — сохранение раскладки во время перезапуска ядра молча пропадало, а
// вкладка со старой копией перетирала чужой «×». Теперь сохранение повторяется, пока ядро недоступно, а слитые
// ядром убранные линии вкладка берёт себе.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SAVE_RETRY_MS, boardState, hideOfflineLine, resetBoard } from '../src/store/board';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function bodyOf(call: unknown): { layout: { hidden_lines: unknown } } {
  return JSON.parse(String((call as [string, RequestInit])[1].body));
}

beforeEach(() => {
  resetBoard();
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
});

afterEach(() => {
  resetBoard();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('сохранение раскладки', () => {
  it('ядро недоступно (нет связи, 502) — сохранение повторяется, пока не пройдёт', async () => {
    const answers = [() => Promise.reject(new TypeError('Failed to fetch')), () => Promise.resolve(json({ detail: 'bad gateway' }, 502))];
    const put = vi.fn(async () => (answers.shift() ?? (() => Promise.resolve(json({ layout: { hidden_lines: [{ key: 'p5:проба:1', ended_ts: 9 }] } }))))());
    vi.stubGlobal('fetch', put);
    hideOfflineLine({ key: 'p5:проба:1', ended_ts: 9 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(put).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(SAVE_RETRY_MS);
    expect(put).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(SAVE_RETRY_MS);
    expect(put).toHaveBeenCalledTimes(3);
    expect(bodyOf(put.mock.calls[2]).layout.hidden_lines).toEqual([{ key: 'p5:проба:1', ended_ts: 9 }]);
    await vi.advanceTimersByTimeAsync(SAVE_RETRY_MS * 3);
    expect(put).toHaveBeenCalledTimes(3);
  });

  it('отказ ядра (422) не повторяется', async () => {
    const put = vi.fn(async () => json({ detail: 'layout too large' }, 422));
    vi.stubGlobal('fetch', put);
    hideOfflineLine({ key: 'a', ended_ts: 1 });
    await vi.advanceTimersByTimeAsync(1000 + SAVE_RETRY_MS * 2);
    expect(put).toHaveBeenCalledTimes(1);
  });

  it('убранные линии из ответа ядра (их убрали в другой вкладке) вкладка берёт себе', async () => {
    const merged = [
      { key: 'p3:клод:1', ended_ts: 4 },
      { key: 'p5:проба:1', ended_ts: 9 },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => json({ layout: { hidden_lines: merged } })));
    hideOfflineLine({ key: 'p5:проба:1', ended_ts: 9 });
    await vi.advanceTimersByTimeAsync(1000);
    expect(boardState.layout.hidden_lines).toEqual(merged);
  });
});
