// Ф47-1 (`docs/architecture.md`–4): данные видов «По проектам» и «Один проект» — чистые
// функции без Vue поверх групп списка слева. Названия и роли — чужой ввод: показываются только текстом.
import { UNSORTED_PROJECT_ID } from '../api/types';
import { sameItem, type ColumnItem } from '../store/board';
import { phoneSessionSub, type ProjectGroup, type RowDot } from './boardList';

/** Одна сессия проекта в строке кнопок: сессия хаба или внешняя линия (в сети или нет). */
export interface ViewRow {
  item: ColumnItem;
  name: string;
  dot: RowDot;
  /** Подпись из строки списка слева. */
  sub: string;
}

export interface ViewProject {
  id: number;
  name: string;
  color: string | null;
  waitCount: number;
  rows: ViewRow[];
}

/** Проекты видов: группы списка слева (тот же порядок) с хотя бы одной сессией; «Неразобранное» и беседы не входят. */
export function viewProjects(groups: ProjectGroup[]): ViewProject[] {
  const out: ViewProject[] = [];
  for (const g of groups) {
    if (g.id === UNSORTED_PROJECT_ID) continue;
    const rows: ViewRow[] = [
      ...g.sessions.map((r) => ({ item: r.item, name: r.name, dot: r.dot, sub: phoneSessionSub(r) })),
      ...g.agents.map((r) => ({ item: r.item, name: r.name, dot: r.dot, sub: r.sub })),
    ];
    if (rows.length === 0) continue;
    out.push({ id: g.id, name: g.id === 0 ? 'Вне проектов' : g.name, color: g.color, waitCount: g.waitCount, rows });
  }
  return out;
}

const DOT_RANK: Record<RowDot, number> = { wait: 0, run: 1, idle: 2 };

/** Какая сессия показана в столбце проекта: выбранная, пока она есть; иначе ждущая, потом работающая, потом первая. */
export function pickedRow(project: ViewProject, picks: Record<string, ColumnItem>): ViewRow {
  const picked = picks[String(project.id)];
  const found = picked === undefined ? undefined : project.rows.find((r) => sameItem(r.item, picked));
  if (found !== undefined) return found;
  let best = project.rows[0]!;
  for (const row of project.rows) if (DOT_RANK[row.dot] < DOT_RANK[best.dot]) best = row;
  return best;
}

/** Проект вида «Один проект»: выбранный, если он ещё есть; иначе первый, у которого что-то ждёт, иначе первый. */
export function resolveViewProject(projects: ViewProject[], id: number | null): ViewProject | null {
  const chosen = id === null ? undefined : projects.find((p) => p.id === id);
  if (chosen !== undefined) return chosen;
  return projects.find((p) => p.waitCount > 0 || p.rows.some((r) => r.dot === 'wait')) ?? projects[0] ?? null;
}
