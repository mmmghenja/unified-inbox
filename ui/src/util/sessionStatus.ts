// Ф41-6 (правки после проверки в браузере): серая строка состояния под переключателем режима в окне сессии:
// «модель · кэш ММ:СС · контекст N тыс. (P %) · $X». Чего нет в данных — того куска нет. Чистые функции без Vue.
import type { HubSession } from '../api/types';
import { compactThreshold, costLabel } from './sessionSteps';

function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

export function sessionStatusParts(session: HubSession, nowMs: number): string[] {
  const parts: string[] = [];
  const model = session.model?.trim();
  if (model) parts.push(model);
  const cache = session.cache;
  if (cache != null && cache.expires_ts > nowMs) parts.push(`кэш ${clock(cache.expires_ts - nowMs)}`);
  const usage = session.usage;
  // usage.context_tokens === null — после сжатия: число из кэша стенограммы устарело, не берём
  const tokens = usage != null ? usage.context_tokens : (cache?.context_tokens ?? null);
  if (usage != null && usage.context_tokens === null) {
    parts.push('контекст уточнится после хода');
  } else if (tokens !== null && tokens !== undefined) {
    const thousands = tokens < 1000 ? String(tokens) : String(Math.round(tokens / 1000));
    // Ф48-2 D: «контекст 132 тыс. из 200» (порог сжатия) или «контекст 132 тыс. · без авто-сжатия»
    const tail =
      session.compact_auto === false
        ? ' · без авто-сжатия'
        : ` из ${Math.round(compactThreshold(session) / 1000)}`;
    parts.push(`контекст ${thousands} тыс.${tail}`);
  }
  if (usage?.cost_usd !== null && usage?.cost_usd !== undefined) parts.push(costLabel(usage.cost_usd));
  // Ф73-1: «Перечитать MCP» нажата и ещё не исполнена хостом.
  if (session.mcp_reload_ts) parts.push(session.state === 'running' ? 'перечитаю MCP после хода' : 'перечитываю MCP…');
  return parts;
}
