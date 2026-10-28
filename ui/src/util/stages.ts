// Ф34-1 A6: стадии бесед. Словаря у хаба нет — стадии проекта считаем по живым (неархивным) беседам из стора,
// поэтому счётчики сходятся со списком и обновляются по `/sync`. Ключ сравнения — без учёта регистра и пробелов
// по краям (ядро сравнивает так же, но точнее; для списка этого достаточно).
import type { Channel } from '../api/types';
import { activityOf } from '../store/selectors';

export const STAGE_MAX = 40;
export const STAGE_NOTE_MAX = 200;

export function stageKey(stage: string | null | undefined): string {
  return (stage ?? '').trim().toLocaleLowerCase();
}

export function hasStage(channel: Channel): boolean {
  return stageKey(channel.stage) !== '';
}

export interface StageGroup {
  key: string;
  /** Написание самой свежей по `stage_ts` беседы ключа (как в ядре). */
  label: string;
  count: number;
  channels: Channel[];
}

export function stageGroups(channels: Channel[]): StageGroup[] {
  const byKey = new Map<string, { label: string; ts: number; channels: Channel[] }>();
  for (const channel of channels) {
    if (channel.archived || !hasStage(channel)) continue;
    const key = stageKey(channel.stage);
    const ts = channel.stage_ts ?? 0;
    const label = (channel.stage ?? '').trim();
    const entry = byKey.get(key);
    if (entry === undefined) {
      byKey.set(key, { label, ts, channels: [channel] });
    } else {
      entry.channels.push(channel);
      if (ts > entry.ts) {
        entry.ts = ts;
        entry.label = label;
      }
    }
  }
  const groups: StageGroup[] = [];
  for (const [key, entry] of byKey) {
    const sorted = entry.channels.slice().sort((a, b) => activityOf(b) - activityOf(a));
    groups.push({ key, label: entry.label, count: sorted.length, channels: sorted });
  }
  return groups.sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

/** Фильтр списка: `all` — все, `none` — без стадии, `s:<ключ>` — одна стадия. */
export type StageFilter = 'all' | 'none' | `s:${string}`;

export function parseStageFilter(raw: string | null, groups: StageGroup[]): StageFilter {
  if (raw === 'none') return 'none';
  if (raw !== null && raw.startsWith('s:')) {
    const key = raw.slice(2);
    // Стадии больше нет в проекте — фильтр не должен прятать весь список.
    if (groups.some((g) => g.key === key)) return `s:${key}`;
  }
  return 'all';
}

export function applyStageFilter(channels: Channel[], filter: StageFilter): Channel[] {
  if (filter === 'all') return channels;
  if (filter === 'none') return channels.filter((c) => !hasStage(c));
  const key = filter.slice(2);
  return channels.filter((c) => stageKey(c.stage) === key);
}

export function stageFilterPrefKey(projectId: number): `hub.${string}` {
  return `hub.stageFilter.${projectId}`;
}

/** Подпись чипа: стадия, с заметкой — во всплывающей подсказке. */
export function stageChipTitle(channel: Channel): string | undefined {
  const note = (channel.stage_note ?? '').trim();
  return note === '' ? undefined : note;
}
