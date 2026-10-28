// Ф48-2 D: окно «Сжатие…» — разбор порога в тысячах токенов и тело `PATCH /sessions/{id}` (только изменённые ключи).
import type { HubSession } from '../api/types';
import { compactThreshold } from './sessionSteps';

export const COMPACT_MIN_K = 50;
export const COMPACT_MAX_K = 900;
export const COMPACT_RANGE_ERROR = `Порог — целое число от ${COMPACT_MIN_K} до ${COMPACT_MAX_K} тыс. токенов.`;

export type CompactPatch = { compact_tokens?: number | null; compact_auto?: boolean };

/** Текст поля «Порог, тыс. токенов» по сохранённому значению сессии (пусто — умолчание). */
export function compactFieldText(session: Pick<HubSession, 'compact_tokens'>): string {
  const tokens = session.compact_tokens ?? null;
  return tokens === null ? '' : String(Math.round(tokens / 1000));
}

/** Тело PATCH из формы. Пустое поле — `null` (умолчание); не целое или вне 50…900 — ошибка на клиенте. */
export function buildCompactPatch(
  session: Pick<HubSession, 'compact_tokens' | 'compact_auto'>,
  auto: boolean,
  fieldText: string,
): { ok: true; body: CompactPatch } | { ok: false; error: string } {
  const text = fieldText.trim();
  let tokens: number | null = null;
  if (text !== '') {
    if (!/^\d+$/.test(text)) return { ok: false, error: COMPACT_RANGE_ERROR };
    const k = Number(text);
    if (k < COMPACT_MIN_K || k > COMPACT_MAX_K) return { ok: false, error: COMPACT_RANGE_ERROR };
    tokens = k * 1000;
  }
  const body: CompactPatch = {};
  if (tokens !== (session.compact_tokens ?? null)) body.compact_tokens = tokens;
  if (auto !== (session.compact_auto ?? true)) body.compact_auto = auto;
  return { ok: true, body };
}

/** Подсказка окна: «Сейчас в контексте N тыс.; сжатие при M тыс.». */
export function compactHint(session: Pick<HubSession, 'usage' | 'compact_at' | 'compact_tokens'>): string {
  const context = session.usage?.context_tokens ?? null;
  const now = context === null ? 'Размер контекста уточнится после хода' : `Сейчас в контексте ${Math.max(1, Math.round(context / 1000))} тыс.`;
  const at = Math.round(compactThreshold(session) / 1000);
  return `${now}; сжатие при ${at} тыс.`;
}
