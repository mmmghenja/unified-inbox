// Ф83-3 (`docs/architecture.md`): запасные движки — чистые функции и тексты без Vue.
// Агент и факты: строка «Запасной» окна «Движок», пометка «на запасном», «Вернуть на основной». Сессии хаба:
// «Запасной» в форме «Модель и усилие», умолчание проекта, чип «на запасном», «Перейти на …» у паузы.
import { ApiError } from '../api/client';
import type { EngineName, EngineStatus, FallbackChoice, FallbackSetting, HubSession, OnFallback } from '../api/types';
import { choiceLabel, type Choice, defaultChoice, ENGINE_NAMES, optionDisabled } from './engine';

/** Повод перехода (`kind` ядра) словами. */
export const FALLBACK_KIND_LABEL: Record<string, string> = {
  limit: 'лимит',
  balance: 'кончился баланс',
  auth: 'нет входа',
  model: 'модели нет',
  retry: 'провайдер не отвечает',
  other: 'сбой',
  refusal: 'модель отказалась',
  transient: 'временный сбой',
};

export const FALLBACK_SAME_ENGINE = 'это основной движок';
export const FALLBACK_FAIL = 'Не удалось — попробуй ещё раз.';

/** «ЧЧ:ММ» по местному времени; `ts` в мс. */
export function hhmm(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function kindLabel(kind: string): string {
  return FALLBACK_KIND_LABEL[kind] ?? kind;
}

function asChoice(value: FallbackChoice | FallbackSetting | null | undefined): Choice | null {
  if (value === null || value === undefined || !ENGINE_NAMES.includes(value.engine)) return null;
  return { engine: value.engine, model: value.model ?? null, effort: value.effort ?? null };
}

/** Запасной агента/фактов для полей выбора (с умолчаниями модели и усилия от ядра); не задан — `null`. */
export function fallbackChoiceOf(status: EngineStatus | null, who: 'agent' | 'facts'): Choice | null {
  if (status === null) return null;
  if (who === 'agent') return asChoice(status.fallback ?? status.agent_fallback);
  return asChoice(status.facts_fallback) ?? asChoice(status.facts?.fallback);
}

/** Запасной факты — свой, а не запасной агента (только у своей записи есть `facts_fallback`). */
export function factsFallbackIsOwn(status: EngineStatus | null): boolean {
  return status?.facts_fallback !== null && status?.facts_fallback !== undefined;
}

/** Пометка блока «на запасном» из ответа ядра: агент — верхний уровень, факты — блок `facts`. */
export function onFallbackOf(status: EngineStatus | null, who: 'agent' | 'facts'): OnFallback | null {
  if (status === null) return null;
  return (who === 'agent' ? status.on_fallback : status.facts?.on_fallback) ?? null;
}

/** Действующий выбор (запасной, пока идёт переход). */
export function effectiveOf(status: EngineStatus | null, who: 'agent' | 'facts'): Choice | null {
  if (status === null) return null;
  return asChoice(who === 'agent' ? status.effective : status.facts?.effective);
}

/** «на запасном: Claude Code · sonnet — лимит с 14:05 (основной: OpenCode · deepseek/deepseek-flash)». */
export function onFallbackLine(on: OnFallback, current: Choice | null): string {
  const now = current === null ? '' : ` ${choiceLabel(current)}`;
  return `на запасном:${now} — ${kindLabel(on.kind)} с ${hhmm(on.since)} (основной: ${choiceLabel(on.main)})`;
}

/** Запасной не может быть основным движком: причина серой кнопки; иначе `null`. */
export function fallbackOptionReason(status: EngineStatus | null, engine: EngineName, main: EngineName | null): string | null {
  if (main !== null && engine === main) return FALLBACK_SAME_ENGINE;
  return optionDisabled(status, engine) ? 'недоступен' : null;
}

/** Первый доступный движок, не равный основному (стартовый выбор «задать запасной»); нет — `null`. Ф85-1: у
 * агента запасным может быть и движок основного с другой моделью — тогда, если другого нет, он. */
export function firstFallbackChoice(status: EngineStatus | null, main: EngineName, kind: 'agent' | 'facts'): Choice | null {
  const engine =
    ENGINE_NAMES.find((e) => e !== main && !optionDisabled(status, e)) ??
    (kind === 'agent' && !optionDisabled(status, main) ? main : undefined);
  return engine === undefined ? null : defaultChoice(status, engine, kind);
}

/** Ф85-1: движок, недоступный запасному блока: у фактов — движок основного; у агента — никакой (тот же движок с
 * другой моделью — законный запасной, совпадение целиком отвергает ядро 422). */
export function fallbackExclude(who: 'agent' | 'facts', main: EngineName): EngineName | undefined {
  return who === 'facts' ? main : undefined;
}

export function fallbackError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return 'Только из интерфейса хаба.';
    if ((err.status === 409 || err.status === 422) && err.message !== '') return err.message;
  }
  return FALLBACK_FAIL;
}

// --- сессии -------------------------------------------------------------------------------------------------

export type SessionFallbackMode = 'inherit' | 'own' | 'none';

/** Своя настройка сессии: `null` — как у проекта, `{engine: null}` — нет, иначе свой. */
export function sessionFallbackMode(session: Pick<HubSession, 'fallback'>): SessionFallbackMode {
  const f = session.fallback;
  if (f === null || f === undefined) return 'inherit';
  return f.engine === null ? 'none' : 'own';
}

export interface FallbackDraft {
  mode: SessionFallbackMode;
  engine: EngineName | '';
  model: string;
  effort: string;
}

/** Черновик формы по сессии (для «своего» — её запись, иначе пусто). */
export function sessionFallbackDraft(session: Pick<HubSession, 'fallback'>): FallbackDraft {
  const mode = sessionFallbackMode(session);
  const f = session.fallback;
  if (mode === 'own' && f !== null && f !== undefined && f.engine !== null) {
    return { mode, engine: f.engine, model: f.model ?? '', effort: f.effort ?? '' };
  }
  return { mode, engine: '', model: '', effort: '' };
}

export type FallbackBody = { engine: EngineName | null; model?: string | null; effort?: string | null };

/** Тело `PATCH` по черновику: `null` — «как у проекта», `undefined` — «свой» без движка (ошибка формы). */
export function fallbackBody(draft: FallbackDraft): FallbackBody | null | undefined {
  if (draft.mode === 'inherit') return null;
  if (draft.mode === 'none') return { engine: null };
  if (draft.engine === '') return undefined;
  return {
    engine: draft.engine,
    model: draft.model.trim() === '' ? null : draft.model.trim(),
    effort: draft.effort === '' ? null : draft.effort,
  };
}

export function sameDraft(a: FallbackDraft, b: FallbackDraft): boolean {
  if (a.mode !== b.mode) return false;
  if (a.mode !== 'own') return true;
  return a.engine === b.engine && a.model.trim() === b.model.trim() && a.effort === b.effort;
}

/** Подсказка чипа «на запасном»: текст ядра, иначе собранный здесь. */
export function sessionFallbackTitle(session: Pick<HubSession, 'on_fallback' | 'on_fallback_text'>): string {
  if (session.on_fallback_text) return session.on_fallback_text;
  const on = session.on_fallback;
  if (on === null || on === undefined) return '';
  return `основной — ${choiceLabel(on.main)}, ${kindLabel(on.kind)}, с ${hhmm(on.since)}`;
}

/** «Перейти на Claude Code · sonnet» у паузы; запасного нет — `null`. */
export function switchLabel(session: Pick<HubSession, 'fallback_target'>): string | null {
  const t = session.fallback_target;
  if (t === null || t === undefined) return null;
  return `Перейти на ${choiceLabel(t)}`;
}

/** Строка про умолчание проекта в форме сессии. */
export function projectFallbackLine(projectFallback: FallbackChoice | null | undefined): string {
  const c = asChoice(projectFallback);
  return c === null ? 'у проекта запасной не задан' : `у проекта: ${choiceLabel(c)}`;
}
