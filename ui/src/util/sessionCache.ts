// Ф41-1: подписи срока кэша промпта и размера контекста сессии Claude Code. Чистые функции от времени `nowMs`.
import type { SessionCache } from '../api/types';

export type CacheTone = 'normal' | 'warn' | 'cold';

const MIN = 60_000;
const WARN_MIN = 10;

export function cacheLabel(cache: SessionCache, nowMs: number): { text: string; tone: CacheTone; title: string } {
  const title = cache.ttl_s === 300 ? 'перерасход: кэш 5 минут' : 'кэш 1 час';
  const left = cache.expires_ts - nowMs;
  if (left > 0) {
    const minutes = Math.ceil(left / MIN);
    return { text: `кэш ${minutes} мин`, tone: minutes > WARN_MIN ? 'normal' : 'warn', title };
  }
  const ago = Math.floor(-left / MIN);
  let text: string;
  if (ago < 60) text = `${ago} мин назад`;
  else if (ago < 24 * 60) text = `${Math.floor(ago / 60)} ч назад`;
  else text = `${Math.floor(ago / (24 * 60))} дн назад`;
  return { text: `кэш остыл ${text}`, tone: 'cold', title };
}

export function contextLabel(tokens: number): string {
  if (tokens < 1000) return `контекст ${tokens}`;
  return `контекст ${Math.round(tokens / 1000)} тыс.`;
}
