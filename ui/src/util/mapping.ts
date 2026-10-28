// Ф14-1 C (`docs/architecture.md`): раскладка бесед по проектам из интерфейса — чистые функции
// выбора проекта в шапке беседы, цели переноса, подписи причины раскладки и видов условий правил.
import type { Channel, MapMoveExample, MapRuleInput, MoveTarget, Project } from '../api/types';

/** Значение пункта «По правилам» в выборе проекта. */
export const MAP_AUTO = 'auto';
/** Значение пункта «Неразобранное» (ручная привязка без проекта). */
export const MAP_UNSORTED = 'none';

/** Потолок пачки `POST /channels/move`. */
export const MOVE_MAX = 200;

export const UNSORTED_NAME = 'Неразобранное';

export interface SelectOption {
  value: string;
  label: string;
}

/** Беседа перенесена вручную? Нет `map_source` (старое ядро) — по `map_reason`. */
export function isManual(channel: Pick<Channel, 'map_source' | 'map_reason'>): boolean {
  if (channel.map_source !== undefined) return channel.map_source === 'manual';
  return channel.map_reason === 'manual';
}

/** Выбранное в шапке: авто -> «По правилам»; ручная -> её проект или «Неразобранное». */
export function projectSelectValue(channel: Pick<Channel, 'map_source' | 'map_reason' | 'project_id'>): string {
  if (!isManual(channel)) return MAP_AUTO;
  return channel.project_id === null ? MAP_UNSORTED : String(channel.project_id);
}

/** Название проекта для людей; `null` и псевдо-проекты — «Неразобранное». */
export function projectName(projects: Project[], projectId: number | null): string {
  if (projectId === null || projectId <= 0) return UNSORTED_NAME;
  const project = projects.find((p) => p.id === projectId);
  if (project === undefined) return `проект #${projectId}`;
  return project.name ?? `проект #${projectId}`;
}

/** Пункты выбора проекта беседы: «По правилам[: сейчас]», настоящие проекты (с папкой — « · Claude»),
 * «Неразобранное». Проекты — в порядке API. */
export function projectSelectOptions(
  projects: Project[],
  channel: Pick<Channel, 'map_source' | 'map_reason' | 'project_id'> | null,
): SelectOption[] {
  const autoLabel =
    channel !== null && !isManual(channel)
      ? `По правилам: ${projectName(projects, channel.project_id)}`
      : 'По правилам';
  return [
    { value: MAP_AUTO, label: autoLabel },
    ...projects
      .filter((p) => p.id > 0)
      .map((p) => ({ value: String(p.id), label: `${p.name ?? `проект #${p.id}`}${p.workdir ? ' · Claude' : ''}` })),
    { value: MAP_UNSORTED, label: UNSORTED_NAME },
  ];
}

/** Пункты «Перенести в…»: проекты, «Неразобранное», «По правилам». */
export function moveTargetOptions(projects: Project[]): SelectOption[] {
  return [
    ...projects.filter((p) => p.id > 0).map((p) => ({ value: String(p.id), label: p.name ?? `проект #${p.id}` })),
    { value: MAP_UNSORTED, label: UNSORTED_NAME },
    { value: MAP_AUTO, label: 'По правилам' },
  ];
}

/** Значение выбора -> цель переноса (`POST /channels/move`); непонятное значение — `undefined`. */
export function targetOf(value: string): MoveTarget | undefined {
  if (value === MAP_AUTO) return 'auto';
  if (value === MAP_UNSORTED) return null;
  const id = Number(value);
  return Number.isInteger(id) && id > 0 ? id : undefined;
}

/** Цель -> поля `PATCH /channels/{id}`. */
export function patchFieldsOf(target: MoveTarget): { project_id: number | null } | { map_source: 'auto' } {
  return target === 'auto' ? { map_source: 'auto' } : { project_id: target };
}

const RULE_REASON_RE = /^правило #\d+: ([\s\S]*)$/;

/** Причина раскладки серым рядом с выбором: `manual` -> «вручную», `правило #N: X` -> «X», `нет правил` ->
 * «правил нет», прочее (`как у форума`, `новая беседа`, …) — как есть. */
export function mapReasonLabel(reason: string | null | undefined): string | null {
  if (reason === null || reason === undefined || reason === '') return null;
  if (reason === 'manual') return 'вручную';
  if (reason === 'нет правил') return 'правил нет';
  const match = RULE_REASON_RE.exec(reason);
  if (match !== null) return match[1];
  return reason;
}

/** Строка «беседу видят сессии Claude проекта «X»» — только если у проекта беседы есть рабочая папка. */
export function claudeSessionsLine(projects: Project[], projectId: number | null): string | null {
  if (projectId === null || projectId <= 0) return null;
  const project = projects.find((p) => p.id === projectId);
  if (project === undefined || !project.workdir) return null;
  return `беседу видят сессии Claude проекта «${project.name ?? `#${project.id}`}»`;
}

export type RuleKind = MapRuleInput['kind'];

export const RULE_KIND_LABELS: Record<RuleKind, string> = {
  folder: 'Папка Telegram',
  label: 'Метка почты',
  domain: 'Отправитель (домен)',
  all: 'Весь аккаунт',
};

function isOne(n: number): boolean {
  return n % 10 === 1 && n % 100 !== 11;
}

/** «1 беседа» («1 беседу» в винительном), «3 беседы», «12 бесед». */
export function besed(n: number, accusative = false): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (isOne(n)) return accusative ? `${n} беседу` : `${n} беседа`;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${n} беседы`;
  return `${n} бесед`;
}

/** Предпросмотр добавления: «Переложит 12 бесед». */
export function addPreviewLine(moved: number): string {
  return `Переложит ${besed(moved, true)}`;
}

/** Предпросмотр удаления: «Удалить правило? 3 беседы переедут:» / «…никто не переедет». */
export function deletePreviewLine(moved: number): string {
  if (moved === 0) return 'Удалить правило? Ни одна беседа не переедет.';
  return `Удалить правило? ${besed(moved)} ${isOne(moved) ? 'переедет' : 'переедут'}:`;
}

/** Значение условия со счётом: «Initech — 12, в Неразобранном 3». */
export function optionCountLabel(name: string, channels: number, unsorted: number): string {
  return unsorted > 0 ? `${name} — ${channels}, в Неразобранном ${unsorted}` : `${name} — ${channels}`;
}

/** Строка предпросмотра: «название: проект → проект». */
export function exampleLine(projects: Project[], example: MapMoveExample): string {
  const title = example.title ?? '(без названия)';
  return `${title}: ${projectName(projects, example.from_project_id)} → ${projectName(projects, example.to_project_id)}`;
}

/** Палитра цветов нового проекта; по умолчанию — первый незанятый. */
export const PROJECT_PALETTE: readonly string[] = [
  '#e53935',
  '#1e88e5',
  '#43a047',
  '#fb8c00',
  '#8e24aa',
  '#00897b',
  '#f4511e',
  '#6d4c41',
];

export function freeColor(projects: Project[]): string {
  const used = new Set(projects.map((p) => (p.color ?? '').toLowerCase()));
  return PROJECT_PALETTE.find((c) => !used.has(c)) ?? PROJECT_PALETTE[0];
}

const COLOR6_RE = /^#[0-9a-f]{6}$/i;

/** Значение для `<input type="color">` (он понимает только `#rrggbb`). */
export function colorInputValue(color: string | null): string {
  return color !== null && COLOR6_RE.test(color) ? color.toLowerCase() : '#888888';
}
