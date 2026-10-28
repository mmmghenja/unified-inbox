// Ф41-5 (интерфейс, `docs/architecture.md`): состояние панели «Автопилот» — каталог
// выключателей, общие значения и переопределения проектов. Ручек ядра может не быть (404) или нет сети — тогда панель
// пуста («ядро ещё не умеет»), ошибок в полосе нет. Отдельный reactive-модуль (как `board.ts`), не часть класса Store.
import { reactive } from 'vue';

import * as api from '../api/client';
import type { AutopilotState, CardRoute } from '../api/types';
import { CARD_ROUTE_BULK_GROUPS } from '../util/owner_channel';
import { store } from './instance';

export const autopilotUi = reactive<{
  /** Панель открыта (меню «⋯» и ссылка «Настроить» в окне сессии). */
  open: boolean;
  data: AutopilotState | null;
  /** Ответ ядра получен хотя бы раз (или не придёт). */
  loaded: boolean;
  error: string | null;
  /** Ядро ответило 404 на `/autopilot` — до перезагрузки страницы не опрашиваем. */
  gone: boolean;
}>({ open: false, data: null, loaded: false, error: null, gone: false });

export function resetAutopilot(): void {
  autopilotUi.open = false;
  autopilotUi.data = null;
  autopilotUi.loaded = false;
  autopilotUi.error = null;
  autopilotUi.gone = false;
}

function isState(v: unknown): v is AutopilotState {
  return v !== null && typeof v === 'object' && Array.isArray((v as AutopilotState).switches);
}

/** Прочитать состояние. 404, сеть, мусор — молча остаётся прежнее (или пусто). */
export async function loadAutopilot(): Promise<void> {
  if (autopilotUi.gone) {
    autopilotUi.loaded = true;
    return;
  }
  try {
    const res = await api.getAutopilot();
    if (isState(res)) autopilotUi.data = res;
  } catch (err) {
    if (err instanceof api.ApiError && err.status === 404) autopilotUi.gone = true;
    // ручки нет или связи нет — панель покажет «ядро ещё не отдаёт выключатели»
  } finally {
    autopilotUi.loaded = true;
  }
}

async function apply(call: () => Promise<AutopilotState>): Promise<boolean> {
  autopilotUi.error = null;
  try {
    const res = await call();
    if (isState(res)) autopilotUi.data = res;
    return true;
  } catch (err) {
    autopilotUi.error = err instanceof api.ApiError && err.message ? err.message : 'Не получилось — попробуй ещё раз.';
    return false;
  }
}

/** Положение выключателя: `scope` — `'global'` или id проекта; `hold`: `true` — придержать, `false` — сама, `null` —
 * как общее (только для проекта). */
export function setSwitch(scope: 'global' | number, key: string, hold: boolean | null): Promise<boolean> {
  return apply(() => api.putAutopilot({ scope, key, hold }));
}

/** Ф66-1: таблица маршрута проекта (`PATCH /projects/{id} {card_route}`, частичное слияние); проекты перечитываются.
 *  Отказ ядра (422) — его текст в `autopilotUi.error`. Работает и с телефона. */
export async function setCardRoute(projectId: number, route: Record<string, CardRoute>): Promise<boolean> {
  autopilotUi.error = null;
  try {
    await api.patchProject(projectId, { card_route: route });
    await store.loadProjects();
    return true;
  } catch (err) {
    autopilotUi.error = err instanceof api.ApiError && err.message ? err.message : 'Не получилось — попробуй ещё раз.';
    return false;
  }
}

/** Ф66-1: «всем: Мне / Агенту» — все группы проекта одним PATCH. Ф93-1: кроме строки «Деньги». */
export function setCardRouteAll(projectId: number, value: 'me' | 'agent'): Promise<boolean> {
  return setCardRoute(projectId, Object.fromEntries(CARD_ROUTE_BULK_GROUPS.map((g) => [g.key, value])));
}

export function holdAll(): Promise<boolean> {
  return apply(() => api.autopilotHoldAll());
}

/** Массово: общее положение у перечисленных выключателей (по одному запросу `PUT`, подряд). Остановка на первой
 * неудаче. Переопределения проектов не трогаются. */
export async function setGlobalMany(keys: string[], hold: boolean): Promise<boolean> {
  for (const key of keys) {
    if (!(await setSwitch('global', key, hold))) return false;
  }
  return true;
}

/** Слова владельца: положение у перечисленных выключателей одного проекта (`hold`: `false` — сама, `true` —
 * спрашивать) — пакетной записи по проекту в ядре нет, по одному `PUT` подряд; остановка на первой неудаче. */
export async function setProjectMany(projectId: number, keys: string[], hold: boolean): Promise<boolean> {
  for (const key of keys) {
    if (!(await setSwitch(projectId, key, hold))) return false;
  }
  return true;
}

/** Число сессионных ключей каталога (M в «придержано N из M»); 0 — каталога нет. */
export function catalogSize(): number {
  // Ключи `scope = "project"` в счёт сессии не входят; старое ядро поля не шлёт — все ключи сессионные.
  return autopilotUi.data?.switches.filter((s) => s.scope !== 'project').length ?? 0;
}
