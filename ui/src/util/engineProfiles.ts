// Ф74-1 (`docs/architecture.md`): профили движков сессий хаба — подписи, усилие, подсказки
// модели, свойства хода. Источник — `GET /status` (`session_engines`); старое ядро поля не отдаёт — тогда
// `DEFAULT_SESSION_ENGINES` (значения до профиля). Глобальный переключатель wake/work (`util/engine.ts`,
// `EnginePanel`) — другой список, он сюда не входит. Источник задаёт `store/instance.ts`
// (`setSessionEnginesSource`): модуль не импортирует стор — его берут и чистые помощники стора (`peerMsgs`).
import type { SessionEngineProfile } from '../api/types';

let source: () => unknown = () => undefined;

/** Откуда брать профили (`store.state.status?.session_engines` единственного стора). */
export function setSessionEnginesSource(get: () => unknown): void {
  source = get;
}

export const DEFAULT_SESSION_ENGINES: readonly SessionEngineProfile[] = [
  {
    name: 'claude',
    label: 'Claude Code',
    short: 'Claude',
    efforts: ['low', 'medium', 'high', 'xhigh', 'max'],
    model_hints: ['sonnet', 'opus', 'haiku'],
    compacts_self: false,
    context_known: true,
    mid_turn: true,
    available: true,
  },
  {
    name: 'opencode',
    label: 'OpenCode',
    short: 'OpenCode',
    efforts: ['low', 'high', 'max'],
    model_hints: ['deepseek/deepseek-flash', 'anthropic/claude-sonnet-5-5'],
    compacts_self: false,
    context_known: true,
    mid_turn: false,
    available: true,
  },
  {
    name: 'cursor',
    label: 'Cursor',
    short: 'Cursor',
    // не ниже high (слово владельца, Ф71-1 П11)
    efforts: ['high', 'xhigh'],
    model_hints: ['grok-4.7', 'claude-sonnet-5-5', 'claude-opus-5-5'],
    compacts_self: true,
    context_known: false,
    mid_turn: true,
    available: true,
    // Ф74-8 (пробы `.runs/f748-probe/`): как в профиле ядра.
    dead_switches: ['cross_session', 'plan_mode'],
    asks_questions: false,
    mid_turn_tool_only: true,
    mcp_statuses: false,
  },
];

function validList(list: unknown): list is SessionEngineProfile[] {
  return (
    Array.isArray(list) &&
    list.length > 0 &&
    list.every((p) => p !== null && typeof p === 'object' && typeof (p as SessionEngineProfile).name === 'string')
  );
}

/** Все профили (в том числе заготовки): из `/status`, иначе запасные. `list` — явный список (тесты). */
export function sessionEngineProfiles(list?: unknown): readonly SessionEngineProfile[] {
  const given = list === undefined ? source() : list;
  return validList(given) ? given : DEFAULT_SESSION_ENGINES;
}

/** Движки, которые интерфейс предлагает для сессии хаба (первый — умолчание). */
export function sessionEngineChoices(list?: unknown): SessionEngineProfile[] {
  const choices = sessionEngineProfiles(list).filter((p) => p.available !== false);
  return choices.length > 0 ? choices : [...DEFAULT_SESSION_ENGINES];
}

/** Профиль движка: из текущего списка, иначе из запасного; неизвестный — `undefined`. */
export function engineProfile(engine: string | null | undefined, list?: unknown): SessionEngineProfile | undefined {
  if (!engine) return undefined;
  return (
    sessionEngineProfiles(list).find((p) => p.name === engine) ??
    DEFAULT_SESSION_ENGINES.find((p) => p.name === engine)
  );
}

/** Подпись движка сессии; неизвестный — `fallback` (без него — само имя). */
export function sessionEngineLabel(engine: string | null | undefined, fallback?: string, list?: unknown): string {
  const profile = engineProfile(engine, list);
  if (profile?.label) return profile.label;
  return fallback ?? String(engine ?? '');
}
