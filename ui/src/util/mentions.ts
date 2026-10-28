// Ф27-1b (docs/architecture.md): упоминания людей Slack в поле ввода и показе.
// Наружу (ядро, Slack) идёт родное `<@U…>`; человеку показывается и в поле набирается `@Имя`. Пара «метка -> id»
// живёт в состоянии поля (не в браузерном хранилище): `toLabels` строит её из имён ядра (`mention_names`), `toWire`
// возвращает `<@id>` каждому оставшемуся в тексте вхождению метки целым словом. Что ядро не знает — оно
// экранирует само, UI на это не полагается и лишнего не подставляет.

import type { MentionPerson } from '../api/types';

export type { MentionPerson };

/** Метка в тексте -> id человека Slack. */
export type MentionMap = Record<string, string>;

const WIRE_RE = /(?<!<)<@([UW][A-Z0-9]{2,20})>(?!>)/g;
const WORD_CHAR = /[\p{L}\p{N}_]/u;

function isWordChar(ch: string | undefined): boolean {
  return ch !== undefined && ch !== '' && WORD_CHAR.test(ch);
}

/** Метка человека: `@Имя`; если такое имя уже занято другим id (в подсказке или в поле) — `@Имя·ник` (без ника — `·id`). */
export function mentionLabel(
  person: MentionPerson,
  siblings: readonly MentionPerson[] = [],
  taken: MentionMap = {},
): string {
  const base = `@${person.name}`;
  const clash =
    siblings.some((p) => p.id !== person.id && p.name === person.name) ||
    (taken[base] !== undefined && taken[base] !== person.id);
  if (!clash) return base;
  const handle = (person.handle ?? '').replace(/^@/, '').trim();
  return `${base}·${handle !== '' ? handle : person.id}`;
}

/** `<@id>` -> `@Имя` по `{id: имя}`; неизвестные id остаются как есть. Два разных id с одним именем получают
 * разные метки (`@Имя`, `@Имя·id`). */
export function toLabels(text: string, names: Record<string, string> | null | undefined): { text: string; map: MentionMap } {
  const map: MentionMap = {};
  if (!names) return { text, map };
  const out = text.replace(WIRE_RE, (whole, id: string) => {
    const name = names[id];
    if (name === undefined || name === '') return whole;
    const label = mentionLabel({ id, name, handle: null }, [], map);
    map[label] = id;
    return label;
  });
  return { text: out, map };
}

/** Только показ: `@Имя` вместо `<@id>`. */
export function showMentions(text: string, names: Record<string, string> | null | undefined): string {
  return toLabels(text, names).text;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** `@Имя` -> `<@id>`: метка целым словом (до — начало строки, пробел или знак, после — конец или не буква/цифра);
 * удалённая или изменённая метка не трогается. Один проход, подставленное повторно не разбирается. */
export function toWire(text: string, map: MentionMap): string {
  const labels = Object.keys(map).sort((a, b) => b.length - a.length);
  if (labels.length === 0) return text;
  const re = new RegExp(labels.map(escapeRe).join('|'), 'gu');
  return text.replace(re, (label, offset: number, whole: string) => {
    const before = offset > 0 ? whole[offset - 1] : undefined;
    const after = whole[offset + label.length];
    if (isWordChar(before) || isWordChar(after)) return label;
    return `<@${map[label]}>`;
  });
}

/** Метки, которых в тексте уже нет, из пары убираются (поле очистили или стёрли имя). */
export function pruneMap(text: string, map: MentionMap): MentionMap {
  const out: MentionMap = {};
  for (const [label, id] of Object.entries(map)) if (text.includes(label)) out[label] = id;
  return out;
}

export const MENTION_QUERY_MAX = 30;

/** `@` + 1–30 символов имени сразу перед кареткой, `@` — в начале строки или после пробела. */
export function findMentionQuery(text: string, caret: number): { start: number; query: string } | null {
  const head = text.slice(0, caret);
  const m = /(^|\s)@([\p{L}\p{N}._-]{1,30})$/u.exec(head);
  if (m === null) return null;
  const query = m[2] ?? '';
  return { start: caret - query.length - 1, query };
}

/** Подстановка метки на место `@запрос` (после неё — пробел, если дальше ещё нет пробела). */
export function insertMention(
  text: string,
  start: number,
  caret: number,
  label: string,
): { text: string; caret: number } {
  const rest = text.slice(caret);
  const gap = rest === '' || /^\s/.test(rest) ? (rest === '' ? ' ' : '') : ' ';
  const next = `${text.slice(0, start)}${label}${gap}${rest}`;
  return { text: next, caret: start + label.length + 1 };
}
