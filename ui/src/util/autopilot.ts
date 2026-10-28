// Ф41-5 (интерфейс, раздел 6, 11 П10/П14 и 12 спеки): чистые функции панели «Автопилот» — группы строк каталога,
// положение ячейки, подписи. Всё из ответа ядра (названия, заметки) — чужой ввод: только текстом.
import type { AutopilotState, AutopilotSwitch, HubSession } from '../api/types';
import { engineProfile, sessionEngineChoices } from './engineProfiles';

/** Мета-выключатели (П10) показываются отдельной группой внизу. */
export const META_GROUP = 'Снимают и остальные ограничения';

/** Постоянные пометки у отдельных строк (П6, П14); `note` ядра показывается рядом серым. */
export const STATIC_NOTES: Record<string, string> = {
  tg_leave: 'необратимо для закрытых групп',
  tg_join: 'от твоего личного аккаунта',
  map_rule: 'стойкое: правило остаётся после выключения авто',
};

export interface SwitchGroup {
  title: string;
  meta: boolean;
  switches: AutopilotSwitch[];
}

/** Группы в порядке каталога ядра; мета-выключатели — одной группой в конце. */
export function groupSwitches(switches: AutopilotSwitch[]): SwitchGroup[] {
  const groups: SwitchGroup[] = [];
  const byTitle = new Map<string, SwitchGroup>();
  const meta: AutopilotSwitch[] = [];
  for (const s of switches) {
    if (s.meta === true) {
      meta.push(s);
      continue;
    }
    let group = byTitle.get(s.group);
    if (group === undefined) {
      group = { title: s.group, meta: false, switches: [] };
      byTitle.set(s.group, group);
      groups.push(group);
    }
    group.switches.push(s);
  }
  if (meta.length > 0) groups.push({ title: META_GROUP, meta: true, switches: meta });
  return groups;
}

/** «карточка» (`ask`) / «запрет» (`deny`) — вид правила в ручном режиме. */
export function manualLabel(s: Pick<AutopilotSwitch, 'manual'>): string {
  return s.manual === 'deny' ? 'запрет' : 'карточка';
}

/** Серые пометки строки: `note` ядра и постоянная пометка (без повторов). */
export function notesOf(s: Pick<AutopilotSwitch, 'key' | 'note'>): string[] {
  const out: string[] = [];
  if (s.note) out.push(s.note);
  const fixed = STATIC_NOTES[s.key];
  if (fixed !== undefined && !out.includes(fixed)) out.push(fixed);
  return out;
}

/** «Общее»: `true` — придержано (по умолчанию стоит). */
export function globalHeld(state: AutopilotState, key: string): boolean {
  return state.global[key] ?? true;
}

export type ProjectCell = 'inherit' | 'hold' | 'free';

/** Ячейка проекта: нет переопределения — «как общее»; `true` — «придержать»; `false` — «сама». */
export function projectCell(state: AutopilotState, projectId: number, key: string): ProjectCell {
  const value = state.projects[String(projectId)]?.[key];
  if (value === undefined || value === null) return 'inherit';
  return value ? 'hold' : 'free';
}

/** Положение ячейки -> тело `PUT`: `true` / `false` / `null`. */
export function cellToHold(cell: ProjectCell): boolean | null {
  return cell === 'hold' ? true : cell === 'free' ? false : null;
}

export const CELL_LABEL: Record<ProjectCell, string> = {
  inherit: 'как общее',
  hold: 'спрашивать',
  free: 'сама',
};

export function metaConfirmText(s: Pick<AutopilotSwitch, 'title'>): string {
  return `«${s.title}» — мета-выключатель: сняв его, сессия сможет снять и остальные ограничения. Снять?`;
}

/** Снятие = «сама» (в общем — флажок убран, в проекте — положение «сама»). */
export function isFreeing(hold: boolean | null): boolean {
  return hold === false;
}

/** Подтверждение включения авто: «придержано N из M» + первые пять названий придержанных. */
export function autoConfirmText(held: number | undefined, total: number): string {
  const count = held === undefined || total === 0 ? '' : ` Сейчас придержано ${held} из ${total}.`;
  return `В авто сессия не спрашивает.${count}`;
}

/** Массовое снятие: одно общее подтверждение со списком названий (мета-выключатели помечены). */
export function bulkFreeConfirmText(switches: Pick<AutopilotSwitch, 'title' | 'meta'>[]): string {
  const list = switches.map((s) => `• ${s.title}${s.meta === true ? ' (мета: снимает и остальные ограничения)' : ''}`);
  return `Снять (общее положение «сама»):\n${list.join('\n')}\n\nСнять?`;
}

/** Слова владельца: «всё сама» у одного проекта — одно подтверждение со списком снимаемого в этом проекте. */
export function projectFreeConfirmText(projectName: string, switches: Pick<AutopilotSwitch, 'title' | 'meta'>[]): string {
  const list = switches.map((s) => `• ${s.title}${s.meta === true ? ' (мета: снимает и остальные ограничения)' : ''}`);
  return `Проект «${projectName}» — снять («сама»):\n${list.join('\n')}\n\nСнять?`;
}

/** Ни один выключатель не снят: авто равен ручному. */
export function autoEqualsManual(held: number | undefined, total: number): boolean {
  return held !== undefined && total > 0 && held >= total;
}

export const AUTO_EQUALS_MANUAL_HINT =
  'Авто сейчас равен ручному: все выключатели автопилота стоят — «⋯ → Автопилот».';

/** Ф74-6: ключ выключателя не действует на движок (`dead_switches` профиля). */
export function switchDeadOn(engine: string, key: string): boolean {
  return (engineProfile(engine)?.dead_switches ?? []).includes(key);
}

/** Ф74-6: строка скрыта, если выключатель не действует ни на одном из предлагаемых движков. */
export function switchRowHidden(key: string): boolean {
  const engines = sessionEngineChoices();
  return engines.length > 0 && engines.every((p) => switchDeadOn(p.name, key));
}

export interface ProjectSwitchView {
  /** Галочка проекта не нужна: на всех живых сессиях проекта выключатель мёртв. */
  hidden: boolean;
  /** «не действует на Codex» — если в проекте есть живая сессия на таком движке (иначе `null`). */
  note: string | null;
}

/** Ф74-6: что показать в ячейке проекта. Показывается, если выключатель действует хотя бы на одном движке живых
 * сессий проекта (сессий нет — показывается без пометки). */
export function projectSwitchView(
  sessions: readonly Pick<HubSession, 'project_id' | 'engine' | 'closed_ts'>[],
  projectId: number,
  key: string,
): ProjectSwitchView {
  const engines = [...new Set(sessions.filter((s) => s.project_id === projectId && s.closed_ts === null).map((s) => s.engine))];
  const dead = engines.filter((e) => switchDeadOn(e, key));
  if (engines.length === 0 || dead.length === 0) return { hidden: false, note: null };
  const labels = dead.map((e) => engineProfile(e)?.short ?? e).join(', ');
  return { hidden: dead.length === engines.length, note: `не действует на ${labels}` };
}
