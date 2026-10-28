// Ф10-6: подменный `matchMedia` для тестов оболочки — ширина окна задаётся числом, смена ширины шлёт `change`
// тем спискам, чей ответ поменялся (как браузер). Не спека — вспомогательный модуль тестов 38/39.
import { FULL_QUERY, PHONE_QUERY, type MatchMediaFn } from '../src/util/layout';

interface FakeMedia {
  matchMedia: MatchMediaFn;
  setWidth(width: number): void;
}

export function fakeMedia(initialWidth: number): FakeMedia {
  let width = initialWidth;
  const lists: { query: string; listeners: Set<() => void>; list: MediaQueryList }[] = [];
  const matches = (query: string): boolean => {
    if (query === FULL_QUERY) return width >= 1280;
    if (query === PHONE_QUERY) return width < 600;
    throw new Error(`неожиданный запрос ${query}`);
  };
  const matchMedia: MatchMediaFn = (query) => {
    const listeners = new Set<() => void>();
    const list = {
      media: query,
      get matches() {
        return matches(query);
      },
      addEventListener: (_type: string, fn: () => void) => listeners.add(fn),
      removeEventListener: (_type: string, fn: () => void) => listeners.delete(fn),
    } as unknown as MediaQueryList;
    lists.push({ query, listeners, list });
    return list;
  };
  return {
    matchMedia,
    setWidth(next) {
      const before = lists.map((l) => matches(l.query));
      width = next;
      lists.forEach((l, i) => {
        if (matches(l.query) !== before[i]) l.listeners.forEach((fn) => fn());
      });
    },
  };
}
