// Ф64-1 (интерфейс, `docs/architecture.md`): рабочий день проекта. Чистые функции без Vue.
import type { Account, Project } from '../api/types';
import { formatTime } from './home';

export const CLOSE_DAY_CONFIRM =
  'Сессии проекта доделают текущий ход и уснут; до «Начать день» их никто не поднимет, «Ждёт тебя» по проекту скрыто.';

export const SESSION_DAY_NOTE = 'спит: день закрыт';

/** Проект «по времени» и день закрыт. */
export function isDayClosed(project: Pick<Project, 'workday' | 'day_open'> | undefined | null): boolean {
  return project?.workday === true && project.day_open === false;
}

/** «день закрыт с ЧЧ:ММ» (время неизвестно — без него); `null` — день открыт или проект не «по времени». */
export function dayClosedLabel(project: Pick<Project, 'workday' | 'day_open' | 'day_ts'> | undefined | null): string | null {
  if (!isDayClosed(project)) return null;
  const ts = project?.day_ts;
  return typeof ts === 'number' ? `день закрыт с ${formatTime(ts)}` : 'день закрыт';
}

/** Id проектов с закрытым днём. */
export function closedDayIds(projects: Project[]): Set<number> {
  return new Set(projects.filter((p) => isDayClosed(p)).map((p) => p.id));
}

/** Ф70-1 (D.1): серая пометка спящего канала — в шапке беседы и у аккаунта в «Раскладке». */
export const CHANNEL_SLEEP_NOTE = 'канал спит — день проекта закрыт';

/** Аккаунт спит вместе с днём своего проекта (поле `sleep` из `GET /status`). */
export function accountAsleep(accounts: Account[] | undefined | null, accountId: number | undefined | null): boolean {
  if (accountId === undefined || accountId === null) return false;
  const account = (accounts ?? []).find((a) => a.id === accountId);
  return account?.sleep !== undefined && account.sleep !== null;
}
