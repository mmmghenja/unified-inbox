// Ф99-8: очерёдность проектов задаёт владелец (`project.sort`); ядро отдаёт `/projects` уже в этом порядке.
// Один помощник на все места интерфейса: «Неразобранное» и «вне проектов» в очерёдности не участвуют.
import type { Project } from '../api/types';

/** Настоящие проекты (id > 0) в порядке очерёдности: по `sort`, при равенстве — по порядку прихода. */
export function orderedProjects(projects: Project[]): Project[] {
  return projects
    .map((p, i) => ({ p, i }))
    .filter(({ p }) => p.id > 0)
    .sort((a, b) => (a.p.sort ?? 0) - (b.p.sort ?? 0) || a.i - b.i)
    .map(({ p }) => p);
}

/** Место проекта в очерёдности; неизвестный — после всех, «вне проектов»/null — самым последним. */
export function projectRank(projects: Project[]): (projectId: number | null) => number {
  const order = new Map(orderedProjects(projects).map((p, i) => [p.id, i]));
  return (projectId) =>
    projectId === null || projectId <= 0
      ? Number.MAX_SAFE_INTEGER
      : (order.get(projectId) ?? Number.MAX_SAFE_INTEGER - 1);
}

/** Список id после сдвига проекта на место выше/ниже; на краю — без изменений (новый массив). */
export function movedIds(ids: number[], id: number, delta: -1 | 1): number[] {
  const from = ids.indexOf(id);
  const to = from + delta;
  const next = [...ids];
  if (from < 0 || to < 0 || to >= ids.length) return next;
  next.splice(from, 1);
  next.splice(to, 0, id);
  return next;
}
