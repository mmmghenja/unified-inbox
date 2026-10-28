// Выборки поверх `SyncState` — F4 приёмки 6b: список каналов проекта вычисляется из `sync.channelsById`
// (а не хранится отдельной копией из REST-ответа), поэтому изменения канала, пришедшие через `/sync`
// (unread, last_msg_ts, title, muted, project_id…), и вовсе новые каналы видны сразу, без повторного захода
// на проект. Правило слоя то же, что у `applySync.ts`: чистая функция, без Vue.
import { ALL_PROJECTS_ID, UNSORTED_PROJECT_ID, type Channel } from '../api/types';
import type { SyncState } from './applySync';

/** Ф66-5: по чему сортируется список бесед — ответ в треде поднимает беседу (нет поля — `last_msg_ts`). */
export function activityOf(c: Channel): number {
  return c.last_activity_ts ?? c.last_msg_ts ?? -1;
}

export function channelsOfProject(sync: SyncState, projectId: number): Channel[] {
  const all = Object.values(sync.channelsById);
  let filtered: Channel[];
  if (projectId === ALL_PROJECTS_ID) {
    filtered = all;
  } else if (projectId === UNSORTED_PROJECT_ID) {
    filtered = all.filter((c) => c.project_id === null);
  } else {
    filtered = all.filter((c) => c.project_id === projectId);
  }
  return filtered.slice().sort((a, b) => activityOf(b) - activityOf(a));
}
