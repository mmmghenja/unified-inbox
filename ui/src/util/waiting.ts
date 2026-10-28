// Ф10-6 B4 (`docs/architecture.md`): «Ждёт» телефона и счётчик вкладки «Главная» — всё, что ждёт
// владельца, со всех проектов. Чистые функции без Vue: только то, что стор уже держит (`home`, `sessions`, `projects`).
//
// Порядок групп: (1) карточки сессий хаба (и протухшие) -> (2) заявки -> (4) черновики -> (5) беседы «нужна
// реакция» (Ф78-1: задач по «Го» — предложений и «ход окончен» — больше нет). Тексты из переписки и от моделей — только строками.
import { projectRank } from './projectOrder';
import type { AgentRequest, GroupOp, ButtonPress, HomeReactItem, HumanRequest, HubSession, JoinRequest, LeaveRequest, OutboxListItem, Project, Reminder, RuleRequest, SessionRequest } from '../api/types';
import { UNSORTED_PROJECT_ID } from '../api/types';
import { heavyCount } from './sessionSub';
import { hasInterrupted } from './sessions';
import type { SessionsState } from '../store/sessions';
import { waitingJoins } from './joins';
import { visibleGroupOps } from './groupOps';
import { visibleLeaves } from './leaves';
import { firedReminders } from './reminders';
import { visibleAgentRequests } from './agentRequests';
import { visibleHumans, isUrgentHuman } from './humanRequests';
import { visiblePresses } from './presses';
import { visibleRuleRequests } from './ruleRequests';
import { closedDayIds } from './workday';

export interface ProjectLabel {
  /** `null` — «Неразобранное». */
  id: number | null;
  name: string;
  color: string | null;
}

export type WaitItem =
  // Ф41-2б-2: ждущая карточка постоянной сессии `request` —
  // `null`, пока список запросов сессии не загружен (тогда число берётся из `pending_requests` записи).
  | { kind: 'session_request'; key: string; project: ProjectLabel; session: HubSession; request: SessionRequest | null; stale: boolean }
  // Ф41-4: сессия не поднимается сама (контекст большой, кэш остыл) — сигналы ждут подъёма владельца.
  | { kind: 'session_heavy'; key: string; project: ProjectLabel; session: HubSession; count: number }
  // Ф48-2 A: у сессии есть прерванная реплика — ждёт решения владельца («Отправить снова» / «Убрать»).
  | { kind: 'session_interrupted'; key: string; project: ProjectLabel; session: HubSession; count: number }
  | { kind: 'join'; key: string; project: ProjectLabel; join: JoinRequest }
  | { kind: 'rule'; key: string; project: ProjectLabel; request: RuleRequest }
  | { kind: 'agent_request'; key: string; project: ProjectLabel; request: AgentRequest }
  | { kind: 'press'; key: string; project: ProjectLabel; press: ButtonPress }
  // Ф54-1: сессия просит человека (капча, вход, код…) — рядом с разрешениями; срок < 10 минут — в самом начале списка.
  | { kind: 'human'; key: string; project: ProjectLabel; human: HumanRequest; urgent: boolean }
  | { kind: 'leave'; key: string; project: ProjectLabel; leave: LeaveRequest }
  | { kind: 'group_op'; key: string; project: ProjectLabel; op: GroupOp }
  | { kind: 'reminder'; key: string; project: ProjectLabel; reminder: Reminder }
  | { kind: 'draft'; key: string; project: ProjectLabel; draft: OutboxListItem }
  | { kind: 'react'; key: string; project: ProjectLabel; item: HomeReactItem };

export interface WaitingInput {
  projects: Project[];
  home: { react: HomeReactItem[]; drafts: OutboxListItem[] };
  /** Ф41-2б-2: постоянные сессии хаба — их ждущие запросы. */
  sessions?: Pick<SessionsState, 'items' | 'pending' | 'stale'>;
  /** Ф17-1b: заявки сессий на вступление (все из стора; видимые отбираются по `nowMs`). */
  joins?: JoinRequest[];
  /** Ф17-2b: заявки на выход из групп и каналов (все из стора; видимые — ждущие и одобренные). */
  leaves?: LeaveRequest[];
  /** Ф62-6: заявки на группы WhatsApp (все из стора; видимые — ждущие и одобренные заявки сессий). */
  groupOps?: GroupOp[];
  /** Ф38-2: заявки на правила раскладки (все из стора; видимые отбираются по `nowMs`). */
  ruleRequests?: RuleRequest[];
  /** Ф41-5: заявки агентов (все из стора; видимые — ждущие). */
  agentRequests?: AgentRequest[];
  /** Ф44-1d: нажатия кнопок ботов (все из стора; видимые — ждущие решения). */
  presses?: ButtonPress[];
  /** Ф54-1: заявки «нужен человек» (все из стора; видимые — ждущие). */
  humans?: HumanRequest[];
  /** Ф34-2: напоминания из стора (сработавшие — в «Ждёт»). */
  reminders?: Reminder[];
  nowMs?: number;
}

const UNSORTED_NAME = 'Неразобранное';

function labelOf(
  projects: Project[],
  projectId: number | null | undefined,
  fallback: { name?: string | null; color?: string | null } = {},
): ProjectLabel {
  if (projectId === null || projectId === undefined || projectId === UNSORTED_PROJECT_ID) {
    return { id: null, name: UNSORTED_NAME, color: null };
  }
  const project = projects.find((p) => p.id === projectId);
  return {
    id: projectId,
    name: project?.name ?? fallback.name ?? `проект ${projectId}`,
    color: project?.color ?? fallback.color ?? null,
  };
}

const projectOrder = projectRank; // Ф99-8: общий помощник очерёдности

/** Всё, что ждёт владельца, в порядке групп. Счётчик вкладки «Ждёт» — длина списка. Ф64-1: элементы проектов с
 * закрытым днём скрыты (их число — `closedDayLines`). */
export function buildWaiting(input: WaitingInput): WaitItem[] {
  const closed = closedDayIds(input.projects);
  const all = buildWaitingAll(input);
  if (closed.size === 0) return all;
  return all.filter((item) => item.project.id === null || !closed.has(item.project.id));
}

export interface ClosedDayLine {
  project: ProjectLabel;
  /** Сколько элементов «Ждёт тебя» скрыто. */
  count: number;
}

/** Ф64-1: тихие строки «<проект>: день закрыт · ждёт N» — по проектам с закрытым днём, где есть скрытое. */
export function closedDayLines(input: WaitingInput): ClosedDayLine[] {
  const closed = closedDayIds(input.projects);
  if (closed.size === 0) return [];
  const counts = new Map<number, ClosedDayLine>();
  for (const item of buildWaitingAll(input)) {
    if (item.project.id === null || !closed.has(item.project.id)) continue;
    const line = counts.get(item.project.id) ?? { project: item.project, count: 0 };
    line.count += 1;
    counts.set(item.project.id, line);
  }
  return [...counts.values()];
}

export function closedDayText(line: ClosedDayLine): string {
  return `${line.project.name}: день закрыт · ждёт ${line.count}`;
}

function buildWaitingAll(input: WaitingInput): WaitItem[] {
  const { projects, home } = input;
  const nowMs = input.nowMs ?? Date.now();
  const orderOf = projectOrder(projects);
  const byProjectThenId = <T extends { project: ProjectLabel }>(a: T, b: T, ida: number, idb: number): number =>
    orderOf(a.project.id) - orderOf(b.project.id) || ida - idb;

  // (1а) ждущие карточки постоянных сессий: загруженные запросы + недостающие по `pending_requests` записи
  const sessionItems: Extract<WaitItem, { kind: 'session_request' }>[] = [];
  const heavyItems: Extract<WaitItem, { kind: 'session_heavy' }>[] = [];
  const interruptedItems: Extract<WaitItem, { kind: 'session_interrupted' }>[] = [];
  const sess = input.sessions;
  if (sess !== undefined) {
    for (const session of Object.values(sess.items)) {
      if (session.closed_ts !== null) continue;
      const project = labelOf(projects, session.project_id);
      const loaded = sess.pending[session.id];
      for (const request of loaded ?? []) {
        if (request.status !== 'pending') continue;
        sessionItems.push({ kind: 'session_request', key: `session-request-${request.id}`, project, session, request, stale: false });
      }
      const missing = loaded === undefined ? session.pending_requests : 0;
      for (let i = 0; i < missing; i += 1) {
        sessionItems.push({ kind: 'session_request', key: `session-wait-${session.id}-${i}`, project, session, request: null, stale: false });
      }
    }
    for (const request of Object.values(sess.stale)) {
      const session = sess.items[request.session_id];
      if (session === undefined) continue;
      sessionItems.push({
        kind: 'session_request',
        key: `session-stale-${request.id}`,
        project: labelOf(projects, session.project_id),
        session,
        request,
        stale: true,
      });
    }
    sessionItems.sort((a, b) => byProjectThenId(a, b, a.request?.id ?? a.session.id, b.request?.id ?? b.session.id));
    for (const session of Object.values(sess.items)) {
      const count = heavyCount(session);
      if (count > 0) {
        heavyItems.push({ kind: 'session_heavy', key: `session-heavy-${session.id}`, project: labelOf(projects, session.project_id), session, count });
      }
    }
    heavyItems.sort((a, b) => byProjectThenId(a, b, a.session.id, b.session.id));
    for (const session of Object.values(sess.items)) {
      if (!hasInterrupted(session)) continue;
      interruptedItems.push({
        kind: 'session_interrupted',
        key: `session-interrupted-${session.id}`,
        project: labelOf(projects, session.project_id),
        session,
        count: session.interrupted_says ?? 0,
      });
    }
    interruptedItems.sort((a, b) => byProjectThenId(a, b, a.session.id, b.session.id));
  }

  // (2а) заявки сессий на вступление в группы и каналы (Ф17-1b): только ждущие решения владельца (исходы — в столбце проекта)
  const joins: WaitItem[] = waitingJoins(input.joins ?? []).map((join) => ({
    kind: 'join' as const,
    key: `join-${join.id}`,
    project: labelOf(projects, join.project_id),
    join,
  }));

  // (2а-1) заявки сессий на правило раскладки (Ф38-2): только ждущие решения владельца (итоги — в столбце проекта)
  const rules: WaitItem[] = visibleRuleRequests(input.ruleRequests ?? [], nowMs)
    .filter((r) => r.status === 'pending')
    .map((request) => ({
    kind: 'rule' as const,
    key: `rule-${request.id}`,
    project: labelOf(projects, request.project_id),
    request,
  }));

  // (2а-2) заявки агентов (Ф41-5): перенос беседы, напарник — ждущие решения
  const agentRequests: WaitItem[] = visibleAgentRequests(input.agentRequests ?? []).map((request) => ({
    kind: 'agent_request' as const,
    key: `agent-request-${request.id}`,
    project: labelOf(projects, request.project_id),
    request,
  }));

  // (2а-3) нажатия кнопок ботов, ждущие владельца (Ф44-1d)
  const presses: WaitItem[] = visiblePresses(input.presses ?? []).map((press) => ({
    kind: 'press' as const,
    key: `press-${press.id}`,
    project: labelOf(projects, press.project_id),
    press,
  }));

  // (1б) заявки «нужен человек» (Ф54-1): со сроком меньше 10 минут — первыми во всём списке, остальные — после
  // запросов и карточек сессий, выше предложений и черновиков
  const humanAll: Extract<WaitItem, { kind: 'human' }>[] = visibleHumans(input.humans ?? [], nowMs).map((human) => ({
    kind: 'human' as const,
    key: `human-${human.id}`,
    project: labelOf(projects, human.project_id),
    human,
    urgent: isUrgentHuman(human, nowMs),
  }));
  const humanUrgent = humanAll.filter((h) => h.urgent);
  const humanRest = humanAll.filter((h) => !h.urgent);

  // (2б) заявки на выход из групп и каналов (Ф17-2b)
  const leaves: WaitItem[] = visibleLeaves(input.leaves ?? []).map((leave) => ({
    kind: 'leave' as const,
    key: `leave-${leave.id}`,
    project: labelOf(projects, leave.project_id),
    leave,
  }));

  // (2б') заявки сессий на группы WhatsApp (Ф62-6)
  const groupOps: WaitItem[] = visibleGroupOps(input.groupOps ?? []).map((op) => ({
    kind: 'group_op' as const,
    key: `group_op-${op.id}`,
    project: labelOf(projects, op.project_id),
    op,
  }));

  // (2в) сработавшие напоминания по беседам (Ф34-2)
  const reminders: WaitItem[] = firedReminders(input.reminders ?? []).map((reminder) => ({
    kind: 'reminder' as const,
    key: `reminder-${reminder.id}`,
    project: labelOf(projects, reminder.project_id),
    reminder,
  }));

  // (4) черновики — старые сверху
  const drafts: WaitItem[] = home.drafts
    .slice()
    .sort((a, b) => a.created_ts - b.created_ts || a.id - b.id)
    .map((draft) => ({
      kind: 'draft' as const,
      key: `draft-${draft.id}`,
      project: labelOf(projects, draft.project_id, { name: draft.project_name }),
      draft,
    }));

  // (5) беседы «нужна реакция» — порядок API
  const react: WaitItem[] = home.react.map((item) => ({
    kind: 'react' as const,
    key: `react-${item.channel.id}`,
    project: labelOf(projects, item.project.id, { name: item.project.name, color: item.project.color }),
    item,
  }));

  return [...humanUrgent, ...sessionItems, ...heavyItems, ...interruptedItems, ...humanRest, ...joins, ...rules, ...agentRequests, ...presses, ...leaves, ...groupOps, ...reminders, ...drafts, ...react];
}

/** Решение 3: счётчик вкладки «Главная» — решения, которые ждут владельца: запросы сессий +
 * черновики + заявки на вступление, ждущие решения (без «ход окончен» и без бесед «нужна реакция» — у бесед свои счётчики на вкладках проектов). */
export function homeDecisionCount(input: WaitingInput): number {
  return buildWaiting(input).filter(
    (i) => i.kind === 'session_request' || i.kind === 'session_heavy' || i.kind === 'session_interrupted' || i.kind === 'draft' || (i.kind === 'join' && i.join.status === 'pending') ||
      (i.kind === 'rule' && i.request.status === 'pending') || i.kind === 'agent_request' || i.kind === 'press' || i.kind === 'human' ||
      (i.kind === 'leave' && i.leave.status === 'pending') || (i.kind === 'group_op' && i.op.status === 'pending') || i.kind === 'reminder',
  ).length;
}
