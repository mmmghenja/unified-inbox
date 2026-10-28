// Ф41-1: подписи срока кэша и контекста.
import { describe, expect, it } from 'vitest';

import type { SessionCache } from '../src/api/types';
import { cacheLabel, contextLabel } from '../src/util/sessionCache';

const NOW = 1_800_000_000_000;
const MIN = 60_000;

function cache(expiresIn: number, ttl = 3600): SessionCache {
  return { last_ts: NOW, ttl_s: ttl, expires_ts: NOW + expiresIn, context_tokens: 0, model: 'm' };
}

describe('cacheLabel', () => {
  it('больше 10 минут — обычный тон', () => {
    expect(cacheLabel(cache(42 * MIN), NOW)).toMatchObject({ text: 'кэш 42 мин', tone: 'normal' });
  });
  it('10 минут и меньше — предупреждение', () => {
    expect(cacheLabel(cache(7 * MIN), NOW)).toMatchObject({ text: 'кэш 7 мин', tone: 'warn' });
    expect(cacheLabel(cache(10 * MIN), NOW).tone).toBe('warn');
  });
  it('остыл — минуты, часы, дни; приглушённый тон', () => {
    expect(cacheLabel(cache(-25 * MIN), NOW)).toMatchObject({ text: 'кэш остыл 25 мин назад', tone: 'cold' });
    expect(cacheLabel(cache(-3 * 60 * MIN), NOW).text).toBe('кэш остыл 3 ч назад');
    expect(cacheLabel(cache(-2 * 24 * 60 * MIN), NOW).text).toBe('кэш остыл 2 дн назад');
  });
  it('ttl 300 — подсказка про перерасход', () => {
    expect(cacheLabel(cache(MIN, 300), NOW).title).toBe('перерасход: кэш 5 минут');
  });
});

describe('contextLabel', () => {
  it('тысячи и малые числа', () => {
    expect(contextLabel(143_000)).toBe('контекст 143 тыс.');
    expect(contextLabel(999)).toBe('контекст 999');
  });
});
