// Ф10-6 A1 (`docs/architecture.md`): главная-доска — столбец на проект. Чистая функция без Vue:
// раскладывает уже загруженное стором (`home.react|drafts|runs|sent`) по проектам.
// Ничего не теряется: реакция и черновики без проекта или с проектом, которого нет среди столбцов, уходят в
// «Неразобранное»; запуски и отправленное всегда с проектом — у неизвестного проекта их нет, иначе
// предупреждение в консоль и пропуск. Отправленное из бесед без проекта — в «Неразобранное» (Ф11-4 И4).
import {
  type AgentRequest,
  type AgentRun,
  type HomeReactItem,
  type ButtonPress,
  type HumanRequest,
  type JoinRequest,
  type RuleRequest,
  type GroupOp,
  type LeaveRequest,
  type OutboxListItem,
  type Project,
  type Reminder,
  UNSORTED_PROJECT_ID,
} from '../api/types';
import type { SessionsState } from '../store/sessions';
import { visibleJoins } from './joins';
import { visibleHumans } from './humanRequests';
import { visiblePresses } from './presses';
import { visibleAgentRequests } from './agentRequests';
import { visibleRuleRequests } from './ruleRequests';
import { visibleGroupOps } from './groupOps';
import { visibleLeaves } from './leaves';
import { firedReminders } from './reminders';
import { sessionWaitCount } from './sessions';
import { closedDayIds } from './workday';

export interface BoardState {
  /** Строка состояния сессии проекта («ждёт тебя», «работает N мин», «свободна»); пусто — сессии нет. */
  line: string;
  /** Сессия ждёт владельца (запрос, вопрос) — пилюля красная. */
  wait: boolean;
}

export interface BoardColumn {
  kind: 'project';
  project: Project;
  /** «Нужна реакция» проекта — порядок API. */
  react: HomeReactItem[];
  /** Черновики проекта — старые сверху. */
  drafts: OutboxListItem[];
  /** Ф12-2: подтверждённые по графику, ждут слота — ближайшие сверху. */
  scheduled: OutboxListItem[];
  /** Ф17-1b: заявки сессий проекта на вступление в группы и каналы — «Вступить?» (видимые, по id). */
  joins: JoinRequest[];
  /** Ф38-2: заявки на правило раскладки — в столбце проекта-цели. */
  ruleRequests: RuleRequest[];
  /** Ф41-5: ждущие заявки агентов проекта. */
  agentRequests: AgentRequest[];
  /** Ф44-1d: ждущие нажатия кнопок ботов — «Нажать?». */
  presses: ButtonPress[];
  /** Ф54-1: ждущие заявки «нужен человек» (срочные первыми). */
  humans: HumanRequest[];
  /** Ф17-2b: просьбы сессий проекта выйти из группы или канала — «Выйти?» (ждущие и одобренные, по id). */
  leaves: LeaveRequest[];
  /** Ф62-6: просьбы сессий проекта по группам WhatsApp (создать, участники, сбросить ссылку). */
  groupOps: GroupOp[];
  /** Ф34-2: сработавшие напоминания проекта (где поставлены) — в «Нужна реакция», по сроку. */
  reminders: Reminder[];
  /** Запуски Claude дня — новые сверху. */
  runs: AgentRun[];
  /** Запуск в работе — самый свежий `running`. */
  running: AgentRun | null;
  /** Отправленное сегодня — порядок API. */
  sent: OutboxListItem[];
  /** Решения, которые ждут владельца: запросы сессий + черновики. */
  waitCount: number;
  state: BoardState;
}

export interface UnsortedColumn {
  kind: 'unsorted';
  react: HomeReactItem[];
  /** Ф34-2: сработавшие напоминания без проекта. */
  reminders: Reminder[];
  drafts: OutboxListItem[];
  scheduled: OutboxListItem[];
  /** Ф11-4 И4: отправленное сегодня из бесед «Неразобранного» (`project_id` null или `-1`) — порядок API. */
  sent: OutboxListItem[];
  /** Черновики «Неразобранного» (у него нет сессий). */
  waitCount: number;
}

export interface Board {
  columns: BoardColumn[];
  unsorted: UnsortedColumn | null;
}

export interface BoardHome {
  react: HomeReactItem[];
  drafts: OutboxListItem[];
  scheduled?: OutboxListItem[];
  /** Ф17-1b: все заявки из стора — видимые отбираются здесь по времени. */
  joins?: JoinRequest[];
  /** Ф38-2: все заявки на правила раскладки из стора — видимые отбираются здесь по времени. */
  ruleRequests?: RuleRequest[];
  /** Ф41-5: все заявки агентов из стора — ждущие отбираются здесь. */
  agentRequests?: AgentRequest[];
  /** Ф44-1d: все нажатия из стора — ждущие отбираются здесь. */
  presses?: ButtonPress[];
  /** Ф54-1: все заявки «нужен человек» из стора — ждущие отбираются здесь. */
  humans?: HumanRequest[];
  /** Ф17-2b: все заявки на выход из стора — видимые отбираются здесь. */
  leaves?: LeaveRequest[];
  groupOps?: GroupOp[];
  /** Ф34-2: все напоминания из стора — сработавшие отбираются здесь. */
  reminders?: Reminder[];
  runs: AgentRun[];
  sent: OutboxListItem[];
}

export function buildBoard(
  projects: Project[],
  home: BoardHome,
  nowMs: number = Date.now(),
  // Ф41-2б-2: ждущие карточки постоянных сессий входят в счётчик «ждёт» проекта.
  sessions?: Pick<SessionsState, 'items' | 'pending'>,
): Board {
  const columns: BoardColumn[] = [];
  const byId = new Map<number, BoardColumn>();
  for (const project of projects) {
    if (project.id <= 0) continue;
    const column: BoardColumn = {
      kind: 'project',
      project,
      react: [],
      drafts: [],
      scheduled: [],
      joins: [],
      ruleRequests: [],
      agentRequests: [],
      presses: [],
      humans: [],
      leaves: [],
      groupOps: [],
      reminders: [],
      runs: [],
      running: null,
      sent: [],
      waitCount: 0,
      state: { line: project.workdir ? 'свободна' : '', wait: false },
    };
    columns.push(column);
    byId.set(project.id, column);
  }
  const unsorted: UnsortedColumn = {
    kind: 'unsorted',
    react: [],
    reminders: [],
    drafts: [],
    scheduled: [],
    sent: [],
    waitCount: 0,
  };
  // Проекты ещё не загружены (первые кадры после входа) — «неизвестный проект» у всех, это не повод для warn.
  const warn = projects.some((p) => p.id > 0) ? (text: string) => console.warn(text) : () => undefined;
  const columnOf = (projectId: number | null | undefined): BoardColumn | undefined =>
    projectId === null || projectId === undefined ? undefined : byId.get(projectId);

  // Ф64-1: день проекта закрыт — ждущие беседы и черновики его столбца не показываются (как «Ждёт тебя»).
  const closed = closedDayIds(projects);
  const quiet = (projectId: number | null | undefined): boolean =>
    projectId !== null && projectId !== undefined && closed.has(projectId);
  for (const item of home.react) {
    if (!quiet(item.project.id)) (columnOf(item.project.id) ?? unsorted).react.push(item);
  }
  const drafts = [...home.drafts].sort((a, b) => a.created_ts - b.created_ts || a.id - b.id);
  for (const item of drafts) {
    if (!quiet(item.project_id)) (columnOf(item.project_id) ?? unsorted).drafts.push(item);
  }
  const scheduled = [...(home.scheduled ?? [])].sort(
    (a, b) => (a.not_before_ts ?? 0) - (b.not_before_ts ?? 0) || a.id - b.id,
  );
  for (const item of scheduled) (columnOf(item.project_id) ?? unsorted).scheduled.push(item);

  for (const join of visibleJoins(home.joins ?? [], nowMs)) {
    const column = columnOf(join.project_id);
    if (column === undefined) {
      warn(`доска: заявка ${join.id} проекта ${join.project_id}, которого нет среди столбцов, — пропущена`);
      continue;
    }
    column.joins.push(join);
  }

  for (const request of visibleRuleRequests(home.ruleRequests ?? [], nowMs)) {
    const column = columnOf(request.project_id);
    if (column === undefined) {
      warn(`доска: заявка на правило ${request.id} проекта ${request.project_id}, которого нет среди столбцов, — пропущена`);
      continue;
    }
    column.ruleRequests.push(request);
  }

  for (const request of visibleAgentRequests(home.agentRequests ?? [])) {
    columnOf(request.project_id)?.agentRequests.push(request);
  }

  for (const press of visiblePresses(home.presses ?? [])) {
    columnOf(press.project_id)?.presses.push(press);
  }

  for (const human of visibleHumans(home.humans ?? [], nowMs)) {
    columnOf(human.project_id)?.humans.push(human);
  }

  for (const leave of visibleLeaves(home.leaves ?? [])) {
    // Заявка владельца из шапки без проекта — столбца нет, карточка не нужна (результат виден в самой беседе).
    const column = columnOf(leave.project_id);
    if (column === undefined) {
      if (leave.project_id !== null) warn(`доска: выход ${leave.id} проекта ${leave.project_id}, которого нет среди столбцов, — пропущен`);
      continue;
    }
    column.leaves.push(leave);
  }

  for (const op of visibleGroupOps(home.groupOps ?? [])) {
    const column = columnOf(op.project_id);
    if (column === undefined) {
      if (op.project_id !== null) warn(`доска: группа ${op.id} проекта ${op.project_id}, которого нет среди столбцов, — пропущена`);
      continue;
    }
    column.groupOps.push(op);
  }

  // Ф34-2: сработавшее напоминание — в столбец проекта, где оно поставлено; без проекта или неизвестного — в
  // «Неразобранное» (чтобы не потерялось).
  for (const reminder of firedReminders(home.reminders ?? [])) {
    (columnOf(reminder.project_id) ?? unsorted).reminders.push(reminder);
  }

  for (const run of home.runs) {
    const column = columnOf(run.project_id);
    if (column === undefined) {
      warn(`доска: запуск ${run.id} проекта ${run.project_id}, которого нет среди столбцов, — пропущен`);
      continue;
    }
    column.runs.push(run);
  }
  for (const item of home.sent) {
    // Ф11-4 И4: отправленное из беседы без проекта — в «Неразобранное»; из неизвестного проекта — пропуск с warn.
    if (item.project_id === null || item.project_id === UNSORTED_PROJECT_ID) {
      unsorted.sent.push(item);
      continue;
    }
    const column = columnOf(item.project_id);
    if (column === undefined) {
      warn(`доска: отправленное ${item.id} без известного проекта — пропущено`);
      continue;
    }
    column.sent.push(item);
  }

  for (const column of columns) {
    column.runs.sort((a, b) => b.started_ts - a.started_ts || b.id - a.id);
    column.running = column.runs.find((r) => r.status === 'running') ?? null;
    const sessionWaits = sessions !== undefined ? sessionWaitCount(sessions, column.project.id) : 0;
    if (sessionWaits > 0 && !column.state.wait) column.state = { line: 'ждёт тебя', wait: true };
    column.waitCount = sessionWaits +
      column.drafts.length +
      column.reminders.length +
      column.joins.filter((j) => j.status === 'pending').length +
      column.ruleRequests.filter((r) => r.status === 'pending').length +
      column.agentRequests.length +
      column.presses.length +
      column.humans.length +
      column.leaves.filter((l) => l.status === 'pending').length +
      column.groupOps.filter((o) => o.status === 'pending').length;
  }
  unsorted.waitCount = unsorted.drafts.length + unsorted.reminders.length;
  const hasUnsorted =
    unsorted.react.length > 0 ||
    unsorted.reminders.length > 0 ||
    unsorted.drafts.length > 0 ||
    unsorted.scheduled.length > 0 ||
    unsorted.sent.length > 0;
  return { columns, unsorted: hasUnsorted ? unsorted : null };
}
