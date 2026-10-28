// Ф58-1 B2: адресаты окна «Написать…» — чистые функции без Vue. Список собирается из тех же групп, что и список слева
// на доске (`buildBoardList`): «Мой агент» первым, дальше проекты в порядке доски (сессии хаба, внешние сессии,
// линии «не в сети»), агенты вне проектов (проект 0) — группой «Вне проектов» в конце. Названия и роли — чужой
// ввод: только текстом.
import type { Machine } from '../api/types';
import type { WriteTarget } from '../store/writeModal';
import type { AgentLine } from '../store/peerMsgs';
import type { ProjectGroup, RowDot } from './boardList';
import { isShowcase } from './showcase';

export interface WriteTargetRow {
  target: WriteTarget;
  name: string;
  /** Подпись одной строкой: состояние / «слушает» / «не в сети: сообщение дождётся». */
  sub: string;
  dot: RowDot;
  /** Непрочитанные ответы адресата владельцу (значок у строки). */
  unread: number;
  offline: boolean;
  /** Ф77-1: машина роли (чип у строки). */
  machine?: Machine | null;
  /** Режим сессии хаба (чип «авто» / подпись «ручной»); у внешних сессий и агентов — нет. */
  mode?: 'auto' | 'manual';
}

export interface WriteTargetGroup {
  id: number;
  name: string;
  rows: WriteTargetRow[];
}

export interface WriteTargets {
  owner_channel: WriteTargetRow;
  groups: WriteTargetGroup[];
}

export const NO_PROJECT_ID = 0;
export const NO_PROJECT_NAME = 'Вне проектов';
export const OFFLINE_SUB = 'не в сети: сообщение дождётся';

export interface WriteTargetsInput {
  groups: ProjectGroup[];
  agentLines: AgentLine[];
  agent: { online: boolean; talk_unread: number };
}

export function buildWriteTargets(input: WriteTargetsInput): WriteTargets {
  const unreadOf = new Map(input.agentLines.map((l) => [l.key, l.unread]));
  const groups: WriteTargetGroup[] = [];
  for (const g of input.groups) {
    const rows: WriteTargetRow[] = [];
    for (const s of g.sessions) {
      rows.push({ target: { kind: 'session', id: s.id }, name: s.name, sub: s.sub, dot: s.dot, unread: 0, offline: false, machine: s.machine ?? null, mode: s.auto ? 'auto' : 'manual' });
    }
    for (const a of g.agents) {
      rows.push({
        target: { kind: 'agent', key: a.key },
        name: a.name,
        sub: a.offline === true ? OFFLINE_SUB : a.sub,
        dot: a.dot,
        unread: unreadOf.get(a.key) ?? 0,
        offline: a.offline === true,
        machine: a.machine ?? null,
      });
    }
    if (rows.length === 0) continue;
    groups.push({ id: g.id, name: g.id === NO_PROJECT_ID ? NO_PROJECT_NAME : g.name, rows });
  }
  // «Вне проектов» — в самом конце.
  groups.sort((a, b) => Number(a.id === NO_PROJECT_ID) - Number(b.id === NO_PROJECT_ID));
  return {
    owner_channel: {
      target: { kind: 'owner_channel' },
      name: 'Мой агент',
      sub: input.agent.online ? 'на связи' : 'не на связи',
      dot: input.agent.online ? 'run' : 'idle',
      unread: input.agent.talk_unread,
      offline: !input.agent.online,
    },
    groups,
  };
}

/** Поиск по имени, подписи и проекту; пустой запрос — всё. «Мой агент» ищется по «агент». */
export function filterWriteTargets(targets: WriteTargets, query: string): WriteTargets {
  const q = query.trim().toLowerCase();
  if (q === '') return targets;
  const hit = (...parts: string[]): boolean => parts.some((p) => p.toLowerCase().includes(q));
  return {
    owner_channel: targets.owner_channel,
    groups: targets.groups
      .map((g) => ({ ...g, rows: hit(g.name) ? g.rows : g.rows.filter((r) => hit(r.name, r.sub)) }))
      .filter((g) => g.rows.length > 0),
  };
}

/** Первый найденный адресат (Enter в поле поиска): «Мой агент», если запрос к нему подходит, иначе первая строка.
 *  Витрина: «Мой агент» не адресат. */
export function firstWriteTarget(targets: WriteTargets, query: string): WriteTarget | null {
  const q = query.trim().toLowerCase();
  const filtered = filterWriteTargets(targets, query);
  if (!isShowcase() && (q === '' || targets.owner_channel.name.toLowerCase().includes(q))) return targets.owner_channel.target;
  return filtered.groups[0]?.rows[0]?.target ?? null;
}

let answerSeq = 0;

/** Ф58-1 C: счётчик для id контейнера ответа в окне карточки (две карточки с одним `ref` не спорят за id). */
export function nextAnswerSeq(): number {
  answerSeq += 1;
  return answerSeq;
}
