// Ф41-6a (`docs/architecture.md`): раскладка «Главной» — что открыто столбцами, сколько их на
// экране, свёрнут ли список и открыта ли панель «Ждёт тебя». Хранит ядро (`GET/PUT /ui/layout`, ключ `ui_layout`);
// пока ручки нет (404) или нет сети — раскладка живёт в памяти страницы, ошибок в полосе нет. Хранилище браузера не
// используется (сторож `tests/11-guard.spec.ts`).
import { reactive } from 'vue';

import * as api from '../api/client';
import type { OfflineLine } from '../api/types';
import { isLineHidden } from '../util/boardList';

// Ф56-3: `owner_channel` — окно «Агент» (очередь владельца); прежний `orch` (чат оркестратора) в сохранённой раскладке читается как он.
export type ColumnKind = 'session' | 'agent' | 'channel' | 'owner_channel';

export interface ColumnItem {
  kind: ColumnKind;
  /** `session` и `channel` — id. */
  id?: number;
  /** `agent` — ключ линии (`p<проект>:<роль>:<номер>` или `s<id подписки>`). */
  key?: string;
}

/** Ф47-1: вид «Главной» — свой набор столбцов, столбец на проект, все сессии одного проекта. */
export type BoardViewKind = 'own' | 'projects' | 'project';

export interface BoardLayout {
  v: 1;
  list_collapsed: boolean;
  columns: number;
  wait_open: boolean;
  open: ColumnItem[];
  hidden_projects: number[];
  /** Убранные из списка линии «не в сети»: ключ и `ended_ts` линии на момент «×» (подписалась и ушла снова — видна опять). */
  hidden_lines: HiddenLine[];
  view: BoardViewKind;
  /** Проект вида «Один проект». */
  view_project: number | null;
  /** Вид «По проектам»: id проекта (строкой) -> показанная в его столбце сессия (`session` или `agent`). */
  picks: Record<string, ColumnItem>;
}

export interface HiddenLine {
  key: string;
  ended_ts: number;
}

export const MAX_HIDDEN_LINES = 100;

export const MAX_ITEMS = 40;
export const MIN_COLUMNS = 1;
export const MAX_COLUMNS = 5;
export const SAVE_DELAY_MS = 800;
/** Повтор сохранения, пока ядро недоступно. */
export const SAVE_RETRY_MS = 5000;
export const FLASH_MS = 1000;

export function defaultLayout(): BoardLayout {
  return { v: 1, list_collapsed: false, columns: 3, wait_open: true, open: [], hidden_projects: [], hidden_lines: [], view: 'own', view_project: null, picks: {} };
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function normalizeItem(entry: Record<string, unknown>): ColumnItem | null {
  switch (entry.kind) {
    case 'session':
    case 'channel':
      return typeof entry.id === 'number' && Number.isInteger(entry.id) ? { kind: entry.kind, id: entry.id } : null;
    case 'agent':
      return typeof entry.key === 'string' && entry.key !== '' ? { kind: 'agent', key: entry.key } : null;
    case 'owner_channel':
    case 'orch':
      return { kind: 'owner_channel' };
    default:
      return null;
  }
}

export function sameItem(a: ColumnItem, b: ColumnItem): boolean {
  return a.kind === b.kind && a.id === b.id && a.key === b.key;
}

/** Ключ столбца для DOM и подсветки. */
export function itemKey(item: ColumnItem): string {
  return `${item.kind}:${item.id ?? item.key ?? ''}`;
}

/** Привести то, что вернуло ядро, к форме раскладки. Лишнее выбрасывается, списки обрезаются до 40. */
export function normalizeLayout(raw: unknown): BoardLayout {
  const out = defaultLayout();
  if (!isRecord(raw)) return out;
  if (typeof raw.list_collapsed === 'boolean') out.list_collapsed = raw.list_collapsed;
  if (typeof raw.wait_open === 'boolean') out.wait_open = raw.wait_open;
  if (typeof raw.columns === 'number' && Number.isFinite(raw.columns)) {
    out.columns = Math.min(MAX_COLUMNS, Math.max(MIN_COLUMNS, Math.round(raw.columns)));
  }
  if (Array.isArray(raw.open)) {
    for (const entry of raw.open) {
      if (!isRecord(entry)) continue;
      const item = normalizeItem(entry);
      if (item !== null && !out.open.some((o) => sameItem(o, item))) out.open.push(item);
      if (out.open.length >= MAX_ITEMS) break;
    }
  }
  if (Array.isArray(raw.hidden_projects)) {
    out.hidden_projects = raw.hidden_projects
      .filter((id): id is number => typeof id === 'number' && Number.isInteger(id))
      .slice(0, MAX_ITEMS);
  }
  if (Array.isArray(raw.hidden_lines)) {
    const seenKeys = new Set<string>();
    for (const entry of raw.hidden_lines) {
      if (!isRecord(entry)) continue;
      if (typeof entry.key !== 'string' || entry.key === '' || seenKeys.has(entry.key)) continue;
      if (typeof entry.ended_ts !== 'number' || !Number.isFinite(entry.ended_ts)) continue;
      seenKeys.add(entry.key);
      out.hidden_lines.push({ key: entry.key, ended_ts: entry.ended_ts });
      if (out.hidden_lines.length >= MAX_HIDDEN_LINES) break;
    }
  }
  if (raw.view === 'projects' || raw.view === 'project') out.view = raw.view;
  if (typeof raw.view_project === 'number' && Number.isInteger(raw.view_project) && raw.view_project >= 1) {
    out.view_project = raw.view_project;
  }
  if (isRecord(raw.picks)) {
    for (const [id, entry] of Object.entries(raw.picks)) {
      if (!/^[1-9]\d*$/.test(id) || !isRecord(entry)) continue;
      const item = normalizeItem(entry);
      if (item === null || (item.kind !== 'session' && item.kind !== 'agent')) continue;
      out.picks[id] = item;
      if (Object.keys(out.picks).length >= MAX_ITEMS) break;
    }
  }
  return out;
}

export const boardState = reactive<{
  layout: BoardLayout;
  /** Ответ ядра получен (или не придёт). */
  loaded: boolean;
  /** Правка до ответа ядра — загруженное не должно её перетереть. */
  dirty: boolean;
  /** Панель «Ждёт» раскрыта на одном проекте (`null` — на всех). Только в памяти. */
  waitProject: number | null;
  /** Ф90-2: панель «Ждёт тебя» поверх доски открыта. Только в памяти: после перезагрузки — закрыта. */
  waitShown: boolean;
  focusKey: string | null;
  focusSeq: number;
  /** Линии «не в сети» (`GET /subs?offline=1`); ручки нет (404) — пусто. */
  offline: OfflineLine[];
  /** Ядро ответило 404 на `/subs?offline=1` — до перезагрузки страницы не опрашиваем. */
  offlineGone: boolean;
}>({ layout: defaultLayout(), loaded: false, dirty: false, waitProject: null, waitShown: false, focusKey: null, focusSeq: 0, offline: [], offlineGone: false });

let saveTimer: ReturnType<typeof setTimeout> | null = null;

/** Тесты: вернуть всё к умолчанию и снять таймер. */
export function resetBoard(): void {
  if (saveTimer !== null) clearTimeout(saveTimer);
  saveTimer = null;
  boardState.layout = defaultLayout();
  boardState.loaded = false;
  boardState.dirty = false;
  boardState.waitProject = null;
  boardState.waitShown = false;
  boardState.focusKey = null;
  boardState.focusSeq = 0;
  boardState.offline = [];
  boardState.offlineGone = false;
}

/** Прочитать раскладку из ядра. 404, сеть, мусор — молча остаётся то, что в памяти. */
export async function loadLayout(): Promise<void> {
  try {
    const res = await api.getUiLayout();
    if (!boardState.dirty && isRecord(res) && isRecord(res.layout)) boardState.layout = normalizeLayout(res.layout);
  } catch {
    // ручки нет или связи нет — раскладка в памяти
  } finally {
    boardState.loaded = true;
  }
}

async function flush(): Promise<void> {
  saveTimer = null;
  const sent = boardState.layout;
  try {
    const res = await api.putUiLayout(sent);
    // Ядро сливает убранные линии с теми, что убрали другие вкладки и телефон, — берём слитое, если с тех пор
    // ничего не меняли. Ответ без списка (старое ядро) список не стирает.
    if (
      saveTimer === null &&
      boardState.layout === sent &&
      isRecord(res) &&
      isRecord(res.layout) &&
      Array.isArray(res.layout.hidden_lines)
    ) {
      boardState.layout.hidden_lines = normalizeLayout(res.layout).hidden_lines;
    }
  } catch (err) {
    // Ядро недоступно (перезапуск: нет связи или 5xx от прослойки) — правка не должна пропасть: повторить позже.
    // 404 и отказ — тишина, как в loadLayout.
    const down = !(err instanceof api.ApiError) || err.status >= 500;
    if (down && saveTimer === null) saveTimer = setTimeout(() => void flush(), SAVE_RETRY_MS);
  }
}

/** Сохранить с задержкой 800 мс после последней правки. */
export function saveLayout(): void {
  if (saveTimer !== null) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => void flush(), SAVE_DELAY_MS);
}

function changed(): void {
  boardState.dirty = true;
  saveLayout();
}

/** Открыть столбец справа; уже открыт — только подсветить и прокрутить к нему (делает `BoardColumns`). */
export function openColumn(item: ColumnItem): void {
  const layout = boardState.layout;
  if (!layout.open.some((o) => sameItem(o, item))) {
    if (layout.open.length >= MAX_ITEMS) layout.open.shift();
    layout.open.push(item);
    changed();
  }
  boardState.focusKey = itemKey(item);
  boardState.focusSeq += 1;
}

/** Заменить столбец другим на том же месте (внешняя сессия забрана в хаб). */
export function replaceColumn(from: ColumnItem, to: ColumnItem): void {
  const list = boardState.layout.open;
  const i = list.findIndex((o) => sameItem(o, from));
  if (i === -1) {
    openColumn(to);
    return;
  }
  const j = list.findIndex((o) => sameItem(o, to));
  if (j !== -1 && j !== i) list.splice(i, 1);
  else list[i] = to;
  changed();
  boardState.focusKey = itemKey(to);
  boardState.focusSeq += 1;
}

/** Линии «не в сети». Любой сбой (404, сеть, мусор) — молча без линий. */
export async function loadOfflineLines(): Promise<void> {
  if (boardState.offlineGone) return;
  try {
    const body = await api.listOfflineLines();
    boardState.offline = Array.isArray(body?.lines)
      ? body.lines.filter((l) => l !== null && typeof l === 'object' && typeof l.key === 'string' && typeof l.sub_id === 'number')
      : [];
  } catch (err) {
    if (err instanceof api.ApiError && err.status === 404) boardState.offlineGone = true;
    boardState.offline = [];
  }
}

export function closeColumn(item: ColumnItem): void {
  const layout = boardState.layout;
  const next = layout.open.filter((o) => !sameItem(o, item));
  if (next.length !== layout.open.length) {
    layout.open = next;
    changed();
  }
}

/** Сдвинуть столбец на `delta` позиций (−1 левее, +1 правее). */
export function moveColumn(item: ColumnItem, delta: number): void {
  const list = boardState.layout.open;
  const i = list.findIndex((o) => sameItem(o, item));
  const j = i + delta;
  if (i === -1 || j < 0 || j >= list.length) return;
  const moved = list.splice(i, 1)[0]!;
  list.splice(j, 0, moved);
  changed();
}

export function setColumns(n: number): void {
  const next = Math.min(MAX_COLUMNS, Math.max(MIN_COLUMNS, Math.round(n)));
  if (next === boardState.layout.columns) return;
  boardState.layout.columns = next;
  changed();
}

export function toggleList(): void {
  boardState.layout.list_collapsed = !boardState.layout.list_collapsed;
  changed();
}

/** `null` — закрыть панель; `'all'` — открыть на всех проектах; число — открыть на одном проекте. */
export function toggleWait(target: number | 'all' | null): void {
  const layout = boardState.layout;
  if (target === null) {
    layout.wait_open = false;
    boardState.waitShown = false;
  } else {
    layout.wait_open = true;
    boardState.waitShown = true;
    boardState.waitProject = target === 'all' ? null : target;
  }
  changed();
}

/** Ф47-1: переключить вид «Главной»; свой набор столбцов (`open`) не трогается. */
export function setView(view: BoardViewKind): void {
  if (boardState.layout.view === view) return;
  boardState.layout.view = view;
  changed();
}

export function setViewProject(id: number): void {
  if (boardState.layout.view_project === id) return;
  boardState.layout.view_project = id;
  changed();
}

/** Вид «По проектам»: показать эту сессию в столбце проекта; подсветка и прокрутка — как у открытого столбца. */
export function pickSession(projectId: number, item: ColumnItem): void {
  const key = String(projectId);
  const cur = boardState.layout.picks[key];
  if (cur === undefined || !sameItem(cur, item)) {
    boardState.layout.picks[key] = item;
    changed();
  }
  boardState.focusKey = `project:${projectId}`;
  boardState.focusSeq += 1;
}

export function hideProject(id: number): void {
  if (boardState.layout.hidden_projects.includes(id)) return;
  boardState.layout.hidden_projects.push(id);
  changed();
}

export function showProject(id: number): void {
  const next = boardState.layout.hidden_projects.filter((p) => p !== id);
  if (next.length === boardState.layout.hidden_projects.length) return;
  boardState.layout.hidden_projects = next;
  changed();
}

/** Убрать из списка слева линию «не в сети» (запись с тем же ключом заменяется); открытый столбец линии закрывается. */
export function hideOfflineLine(line: Pick<OfflineLine, 'key' | 'ended_ts'>): void {
  const entry: HiddenLine = { key: line.key, ended_ts: line.ended_ts ?? 0 };
  const rest = boardState.layout.hidden_lines.filter((h) => h.key !== line.key);
  boardState.layout.hidden_lines = [...rest, entry].slice(-MAX_HIDDEN_LINES);
  const column: ColumnItem = { kind: 'agent', key: line.key };
  const open = boardState.layout.open.filter((o) => !sameItem(o, column));
  if (open.length !== boardState.layout.open.length) boardState.layout.open = open;
  changed();
}

/** Убрать все линии «не в сети» проекта разом. */
export function hideOfflineLines(projectId: number): void {
  const mine = boardState.offline.filter((l) => l.project_id === projectId && !isLineHidden(l, boardState.layout.hidden_lines));
  if (mine.length === 0) return;
  const keys = new Set(mine.map((l) => l.key));
  const rest = boardState.layout.hidden_lines.filter((h) => !keys.has(h.key));
  boardState.layout.hidden_lines = [...rest, ...mine.map((l) => ({ key: l.key, ended_ts: l.ended_ts ?? 0 }))].slice(-MAX_HIDDEN_LINES);
  boardState.layout.open = boardState.layout.open.filter((o) => !(o.kind === 'agent' && o.key !== undefined && keys.has(o.key)));
  changed();
}

/** Убрать столбцы сессий хаба, которых больше нет (закрыта). Зовётся после загрузки списка сессий. */
export function pruneSessions(existing: Set<number>): void {
  const next = boardState.layout.open.filter((o) => o.kind !== 'session' || (o.id !== undefined && existing.has(o.id)));
  if (next.length !== boardState.layout.open.length) {
    boardState.layout.open = next;
    changed();
  }
}
