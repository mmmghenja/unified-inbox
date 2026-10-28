// Ф48-1: фоновые агенты и команды сессии хаба — чип «агенты: N работают», состояние «ждёт агентов», порядок и
// подписи окна со списком, текст подтверждения «Усыпить». Чистые функции без Vue. Названия и итоги задач — чужой
// ввод: показывать только текстом.
import type { HubSession, SessionBgItem } from '../api/types';

export interface BgCounts {
  agents: number;
  commands: number;
  /** Всего записей (живых и законченных). */
  total: number;
}

export function bgCounts(session: Pick<HubSession, 'bg'>): BgCounts {
  const items = session.bg?.items ?? [];
  let agents = 0;
  let commands = 0;
  for (const item of items) {
    if (item.state !== 'running') continue;
    if (item.kind === 'agent') agents += 1;
    else commands += 1;
  }
  return { agents, commands, total: items.length };
}

/** Есть живые задачи (агенты или команды). */
export function hasLiveBg(session: Pick<HubSession, 'bg'>): boolean {
  const c = bgCounts(session);
  return c.agents + c.commands > 0;
}

/** Текст чипа: «агенты: 2 работают» / «фон: 1» / «агенты: 2 · фон: 1»; живых нет, законченные есть — «агенты: готово»;
 * задач нет — `null`. */
export function bgChipText(session: Pick<HubSession, 'bg'>): string | null {
  const c = bgCounts(session);
  const parts: string[] = [];
  if (c.agents > 0) parts.push(`агенты: ${c.agents}`);
  if (c.commands > 0) parts.push(`фон: ${c.commands}`);
  if (parts.length > 0) return parts.join(' · ');
  return c.total > 0 ? 'агенты: готово' : null;
}

/** Состояние сессии одним словом: `idle` при живых задачах — «ждёт агентов». */
export function sessionStateText(
  session: Pick<HubSession, 'state' | 'bg'>,
  labels: Record<HubSession['state'], string>,
): string {
  if (session.state === 'idle' && hasLiveBg(session)) return 'ждёт агентов';
  return labels[session.state];
}

export const BG_STATE_LABEL: Record<string, string> = {
  running: 'работает',
  completed: 'готово',
  failed: 'ошибка',
  stopped: 'остановлен',
};

export function bgStateLabel(state: string): string {
  return BG_STATE_LABEL[state] ?? state;
}

export function bgKindLabel(item: Pick<SessionBgItem, 'kind' | 'agent_type'>): string {
  if (item.kind === 'command') return 'команда';
  return item.agent_type?.trim() ? item.agent_type.trim() : 'агент';
}

export interface BgRow {
  item: SessionBgItem;
  /** 0 — верхний уровень, 1 — задача подагента. */
  depth: number;
}

/** Порядок окна: сначала живые, затем законченные, новые выше; задачи подагента — сразу под ним, с отступом.
 * Родитель, которого нет в списке, не делает задачу вложенной. Ссылки по кругу не зацикливают и не теряют записи. */
export function bgRows(items: readonly SessionBgItem[]): BgRow[] {
  const ids = new Set(items.map((i) => i.id));
  const live = (i: SessionBgItem): number => (i.state === 'running' ? 0 : 1);
  const byOrder = (a: SessionBgItem, b: SessionBgItem): number => live(a) - live(b) || b.started_ts - a.started_ts;
  const children = new Map<string, SessionBgItem[]>();
  const roots: SessionBgItem[] = [];
  for (const item of items) {
    if (item.parent !== null && item.parent !== item.id && ids.has(item.parent)) {
      const list = children.get(item.parent) ?? [];
      list.push(item);
      children.set(item.parent, list);
    } else {
      roots.push(item);
    }
  }
  const rows: BgRow[] = [];
  const seen = new Set<string>();
  for (const root of [...roots].sort(byOrder)) {
    rows.push({ item: root, depth: 0 });
    seen.add(root.id);
    for (const child of [...(children.get(root.id) ?? [])].sort(byOrder)) {
      rows.push({ item: child, depth: 1 });
      seen.add(child.id);
    }
  }
  for (const item of [...items].sort(byOrder)) if (!seen.has(item.id)) rows.push({ item, depth: 0 });
  return rows;
}

/** «12 с» / «1 мин 05 с» / «1 ч 02 мин». */
export function bgDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total} с`;
  const m = Math.floor(total / 60);
  if (m < 60) return `${m} мин ${String(total % 60).padStart(2, '0')} с`;
  return `${Math.floor(m / 60)} ч ${String(m % 60).padStart(2, '0')} мин`;
}

/** Сколько идёт задача: живая — от `started_ts` до сейчас, законченная — до `ended_ts`. */
export function bgElapsed(item: Pick<SessionBgItem, 'state' | 'started_ts' | 'ended_ts'>, nowMs: number): string {
  const end = item.state === 'running' || item.ended_ts === null ? nowMs : item.ended_ts;
  return bgDuration(end - item.started_ts);
}

export function bgTokens(tokens: number): string {
  return tokens < 1000 ? String(tokens) : `${Math.round(tokens / 1000)} тыс.`;
}

/** Подтверждение «Усыпить» при живых задачах; без них — `null` (подтверждения нет). */
export function bgSleepConfirm(session: Pick<HubSession, 'bg'>): string | null {
  const c = bgCounts(session);
  if (c.agents + c.commands === 0) return null;
  return `Работают агенты: ${c.agents}, фоновых команд: ${c.commands} — они будут остановлены`;
}
