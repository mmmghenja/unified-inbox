// Ф75-1 (docs/architecture.md.3): модель агента владельца в «Написать…» → «Мой агент».
// Чистые функции подписи и выбора уровня — компонент `AgentModelPicker` только рисует.
import { ApiError } from '../api/client';
import type { OwnerAgentModel, OwnerAgentSettings } from '../api/types';

/** Модель и уровень, которые действуют сейчас: выбор владельца или умолчание службы. */
export function agentModelCurrent(s: OwnerAgentSettings): { model: string; variant: string | null; chosen: boolean } {
  if (s.model !== null) return { model: s.model, variant: s.variant, chosen: true };
  return { model: s.default.model, variant: s.default.variant, chosen: false };
}

/** Строка в шапке: «deepseek/deepseek-flash · max», без выбора владельца — с «(по умолчанию)». */
export function agentModelLabel(s: OwnerAgentSettings): string {
  const cur = agentModelCurrent(s);
  const head = cur.variant === null || cur.variant === '' ? cur.model : `${cur.model} · ${cur.variant}`;
  return cur.chosen ? head : `${head} (по умолчанию)`;
}

export function agentModelEntry(models: OwnerAgentModel[], id: string): OwnerAgentModel | null {
  const key = id.trim();
  return models.find((m) => m.id === key) ?? null;
}

/** Подпись строки списка: имя модели и пометка «картинки», если модель их принимает. */
export function agentModelOption(m: OwnerAgentModel): string {
  const name = m.name !== null && m.name !== '' && m.name !== m.id ? m.name : '';
  const parts = [name, m.images === true ? 'картинки' : ''].filter((p) => p !== '');
  return parts.join(' · ');
}

/** Уровни для выбранной модели: из списка OpenCode; модели нет в списке (свободный ввод) — `null` (поле текстом). */
export function agentVariantChoices(models: OwnerAgentModel[], id: string): string[] | null {
  const entry = agentModelEntry(models, id);
  return entry === null ? null : entry.variants;
}

/** Уровень после смены модели: не из её списка — «по умолчанию» (пусто). */
export function agentVariantFor(models: OwnerAgentModel[], id: string, variant: string): string {
  const choices = agentVariantChoices(models, id);
  if (choices === null) return variant;
  return choices.includes(variant) ? variant : '';
}

const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._:@+/-]*$/;

/** Проверка до отправки. OpenCode — форма ядра («провайдер/модель»); другие движки (Ф78-2c) — идентификатор
 * без пробелов (`sonnet`, `gpt-6-luna`), список и допустимость проверяет ядро. Пусто — снять выбор (годится). */
export function agentModelInputError(id: string, engine: string = 'opencode'): string | null {
  const v = id.trim();
  if (v === '') return null;
  if (engine !== 'opencode') {
    return /^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$/.test(v) ? null : 'модель пишется без пробелов';
  }
  return MODEL_RE.test(v) ? null : 'модель пишется как «провайдер/модель», без пробелов';
}

export function agentSettingsError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return 'менять модель агента можно только из интерфейса хаба';
    if (err.status === 422) return err.message !== '' ? `ядро не приняло: ${err.message}` : 'ядро не приняло: проверь модель и уровень';
    return `не сохранилось: ${err.message}`;
  }
  return 'не сохранилось: нет связи с хабом';
}
