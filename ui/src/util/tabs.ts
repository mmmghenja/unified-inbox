// Ф10-6 B2 (решение 3): вкладки проектов верхней полосы и чипы проектов «Бесед» телефона. Чистая функция:
// проекты с `id > 0` — в порядке API, затем «Неразобранное» (`-1`) со счётчиком, затем «Все» (`-2`) — без
// счётчика, приглушённо. Счётчик — как прежде в списке проектов: красный «реагирую», серый прочее (`unreadBadge`).
import type { Project } from '../api/types';
import { ALL_PROJECTS_ID, UNSORTED_PROJECT_ID } from '../api/types';
import { unreadBadge, type UnreadBadge } from './attention';

export type ProjectTabKind = 'project' | 'unsorted' | 'all';

export interface ProjectTab {
  id: number;
  kind: ProjectTabKind;
  name: string;
  color: string | null;
  badge: UnreadBadge | null;
}

function tabOf(project: Project, kind: ProjectTabKind): ProjectTab {
  return {
    id: project.id,
    kind,
    name: project.name ?? (kind === 'unsorted' ? 'Неразобранное' : kind === 'all' ? 'Все' : '(без названия)'),
    color: kind === 'project' ? project.color : null,
    badge: kind === 'all' ? null : unreadBadge(project.unread_react, project.unread),
  };
}

/** Вкладки после «Главной»: проекты, «Неразобранное», «Все». */
export function projectTabs(projects: Project[]): ProjectTab[] {
  const real = projects.filter((p) => p.id > 0).map((p) => tabOf(p, 'project'));
  const unsorted = projects.find((p) => p.id === UNSORTED_PROJECT_ID);
  const all = projects.find((p) => p.id === ALL_PROJECTS_ID);
  return [
    ...real,
    ...(unsorted !== undefined ? [tabOf(unsorted, 'unsorted')] : []),
    ...(all !== undefined ? [tabOf(all, 'all')] : []),
  ];
}

/** Чипы «Бесед» телефона: «Все» · проекты · «Неразобранное». */
export function projectChips(projects: Project[]): ProjectTab[] {
  const tabs = projectTabs(projects);
  return [...tabs.filter((t) => t.kind === 'all'), ...tabs.filter((t) => t.kind !== 'all')];
}
