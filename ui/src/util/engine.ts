// Ф16-1 / Ф78-2c (`docs/architecture.md`, 12, 13): выбор «движок · модель ·
// усилие» — чистые функции и тексты без Vue. Выборов два: «Мой агент» (`PUT /engine`) и «Факты о людях»
// (`PUT /engine/facts`, пока записи нет — факты идут за агентом). Таблицы ролей и задач по «Го» больше нет.
import { ApiError } from '../api/client';
import type { EngineInfo, EngineModel, EngineName, EngineStatus } from '../api/types';
import { DEFAULT_SESSION_ENGINES } from './engineProfiles';

// Ф74-1: подписи — одна копия, запасные профили движков (`util/engineProfiles.ts`); подписи сессий хаба по
// данным ядра — `sessionEngineLabel`.
export const ENGINE_LABEL = { codex: 'Codex', ...Object.fromEntries(DEFAULT_SESSION_ENGINES.map((p) => [p.name, p.label])) } as Record<
  EngineName,
  string
>;

/** Короткая подпись чипа в верхней полосе телефона. */
export const ENGINE_SHORT = Object.fromEntries(DEFAULT_SESSION_ENGINES.map((p) => [p.name, p.short])) as Record<
  EngineName,
  string
>;

export const ENGINE_NAMES: EngineName[] = ['claude', 'opencode', 'codex', 'cursor'];

/** Старое ядро (до 78-2a) отвечало 403 с телефона. */
export const PHONE_HINT = 'Это ядро ещё не принимает выбор движка с телефона — обнови хаб';

export const SWITCH_FAIL = 'Не удалось переключить — попробуй ещё раз.';

/** Предупреждение у модели агента без картинок: «Реши» с браузером шлёт снимки экрана. */
export const NO_IMAGES_WARN = '«Реши» с браузером на ней не сработает: она не видит картинки';

export interface Choice {
  engine: EngineName;
  model: string | null;
  effort: string | null;
}

/** Данные движка из ответа ядра; нет (старое ядро, не загружено) — `null`. */
export function engineInfo(status: EngineStatus | null, engine: EngineName): EngineInfo | null {
  return status?.engines?.find((e) => e.name === engine) ?? null;
}

/** Движок нельзя выбрать: данных нет (не загружено) или ядро говорит «недоступен». */
export function optionDisabled(status: EngineStatus | null, engine: EngineName): boolean {
  const info = engineInfo(status, engine);
  return info === null || !info.available;
}

/** Почему движок недоступен — текст ядра как есть; `null` — доступен. */
export function optionReason(status: EngineStatus | null, engine: EngineName): string | null {
  if (!optionDisabled(status, engine)) return null;
  return engineInfo(status, engine)?.reason ?? 'недоступен';
}

/** Чей выбор: агент (`models`, `default_*`) или факты (`facts_models`, `facts_default_*`; нет полей — как у агента). */
export type ChoiceKind = 'agent' | 'facts';

export function modelsOf(info: EngineInfo | null, kind: ChoiceKind = 'agent'): EngineModel[] {
  if (info === null) return [];
  return kind === 'facts' ? (info.facts_models ?? info.models) : info.models;
}

function defaultsOf(info: EngineInfo | null, kind: ChoiceKind): { model: string | null; effort: string | null } {
  if (info === null) return { model: null, effort: null };
  if (kind === 'facts' && info.facts_default_model !== undefined) {
    return { model: info.facts_default_model, effort: info.facts_default_effort ?? null };
  }
  return { model: info.default_model, effort: info.default_effort };
}

export function modelEntry(
  info: EngineInfo | null,
  model: string | null,
  kind: ChoiceKind = 'agent',
): EngineModel | null {
  if (model === null) return null;
  return modelsOf(info, kind).find((m) => m.id === model) ?? null;
}

/** Усилия выбранной модели; у модели список пуст — усилия движка. */
export function effortsFor(info: EngineInfo | null, model: string | null, kind: ChoiceKind = 'agent'): string[] {
  if (info === null) return [];
  const own = modelEntry(info, model, kind)?.efforts ?? [];
  return own.length > 0 ? own : info.efforts;
}

/** Усилие, допустимое для модели: не из списка — умолчание движка (если оно в списке), иначе `null` («по
 * умолчанию»: у Claude усилие по умолчанию — это отсутствие флага, подставлять первое нельзя). */
export function effortOrDefault(
  info: EngineInfo | null,
  model: string | null,
  effort: string | null,
  kind: ChoiceKind = 'agent',
): string | null {
  const list = effortsFor(info, model, kind);
  if (list.length === 0) return null;
  if (effort !== null && list.includes(effort)) return effort;
  const def = defaultsOf(info, kind).effort;
  if (def !== null && list.includes(def)) return def;
  return null;
}

/** Выбор по умолчанию для движка: выбор владельца, иначе умолчание движка. */
export function defaultChoice(status: EngineStatus | null, engine: EngineName, kind: ChoiceKind = 'agent'): Choice {
  const info = engineInfo(status, engine);
  const def = defaultsOf(info, kind);
  // Выбор владельца (`model`/`effort` движка) — выбор агента; у фактов свои умолчания.
  const model = kind === 'agent' ? (info?.model ?? def.model) : def.model;
  const effort = kind === 'agent' ? (info?.effort ?? def.effort) : def.effort;
  return { engine, model, effort: effortOrDefault(info, model, effort, kind) };
}

/** Выбор после смены модели: усилие подгоняется под её список. */
export function afterModel(
  status: EngineStatus | null,
  draft: Choice,
  model: string,
  kind: ChoiceKind = 'agent',
): Choice {
  return { ...draft, model, effort: effortOrDefault(engineInfo(status, draft.engine), model, draft.effort, kind) };
}

/** Модель видит картинки? `null` — не знаем (модель не в списке). */
export function modelSeesImages(status: EngineStatus | null, choice: Choice): boolean | null {
  return modelEntry(engineInfo(status, choice.engine), choice.model)?.images ?? null;
}

/** «OpenCode · deepseek/deepseek-flash · max». */
export function choiceLabel(choice: Choice): string {
  return [ENGINE_LABEL[choice.engine], choice.model, choice.effort].filter((p) => p !== null && p !== '').join(' · ');
}

/** Выбор агента из ответа ядра. */
export function agentChoice(status: EngineStatus | null): Choice | null {
  if (status === null) return null;
  return { engine: status.engine, model: status.model ?? null, effort: status.effort ?? null };
}

/** Выбор фактов из ответа ядра; блока нет (старое ядро) — факты идут за агентом. */
export function factsChoice(status: EngineStatus | null): (Choice & { follows: boolean }) | null {
  const agent = agentChoice(status);
  if (agent === null || status === null) return null;
  const f = status.facts;
  if (f === undefined) return { ...agent, follows: true };
  return { engine: f.engine, model: f.model ?? null, effort: f.effort ?? null, follows: f.follows_agent };
}

export function same(a: Choice | null, b: Choice | null): boolean {
  return a !== null && b !== null && a.engine === b.engine && a.model === b.model && a.effort === b.effort;
}

/** Подтверждение смены агента; факты упоминаются, только если они идут за агентом. */
export function confirmAgentText(choice: Choice, factsFollow: boolean): string {
  const lines = [`Агент владельца перейдёт на ${choiceLabel(choice)}.`];
  if (factsFollow) lines.push('Факты о людях идут за агентом — они перейдут туда же.');
  lines.push('Подписанные сессии проектов не затрагиваются.');
  return lines.join('\n');
}

export function confirmFactsText(choice: Choice | null): string {
  return choice === null
    ? 'Факты о людях снова пойдут за агентом.'
    : `Факты о людях перейдут на ${choiceLabel(choice)}.`;
}

/** Строка после успешной смены. */
export function switchedNotice(choice: Choice | null, what: 'agent' | 'facts'): string {
  const head = what === 'agent' ? 'Агент' : 'Факты';
  return choice === null ? `${head}: как у агента.` : `${head}: ${choiceLabel(choice)}.`;
}

/** Неудача смены: 409/422 — причина ядра, 403 — старое ядро не пускает. */
export function switchError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return PHONE_HINT;
    if ((err.status === 409 || err.status === 422) && err.message !== '') return err.message;
  }
  return SWITCH_FAIL;
}
