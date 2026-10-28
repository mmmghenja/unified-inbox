// Ф41-6a (`docs/architecture.md`): данные списка слева и полосы «Ждёт тебя» — чистые функции без
// Vue. Названия, роли и заголовки бесед — чужой ввод: показываются только текстом.
import { projectRank } from './projectOrder';
import { bgChipText, hasLiveBg } from './sessionBg';
import type { HubSession, Machine, OfflineLine, Project, ProjectSub, SessionRequest } from '../api/types';
import { UNSORTED_PROJECT_ID } from '../api/types';
import type { AgentLine } from '../store/peerMsgs';
import type { HomeData } from '../store/store';
import { engineLabel, listeningLabel } from '../store/peerMsgs';
import type { ColumnItem, HiddenLine } from '../store/board';
import { sessionEngineLabel } from './engineProfiles';
import { INTERRUPTED_STATE, hasInterrupted, modelChipText, sessionName } from './sessions';
import type { WaitItem } from './waiting';
import { closedDayIds } from './workday';

export type RowDot = 'wait' | 'run' | 'idle';

export interface SessionRow {
  item: ColumnItem;
  id: number;
  name: string;
  dot: RowDot;
  /** Подпись одной строкой: «ждёт тебя» / «спрашивает» / «работает» / «спит» … */
  sub: string;
  auto: boolean;
  /** Ф56: у сессии включён «канал владельца». */
  /** Ф50-7: подпапка сессии (полный путь) или `null`. */
  subdir: string | null;
  /** Модель из записи сессии (пусто — не показывается). */
  model: string | null;
  /** Ф98-1: «движок · модель [· усилие]» второй строкой под ролью; пустая модель — «модель по умолчанию». */
  engineModel: string;
  /** «кэш ММ:СС» красным, когда срок меньше 10 минут при контексте больше 100 тыс. */
  cacheWarn: string | null;
  /** Ф50-4: «карточка у <роль> до ЧЧ:ММ» (серым), если карточки сессии у делегата; иначе `null`. */
  /** Ф77-1: машина сессии (у сессии хаба — машина ядра). */
  machine?: Machine | null;
}

export interface AgentRow {
  /** Ф77-1: машина подписки. */
  machine?: Machine | null;
  item: ColumnItem;
  key: string;
  name: string;
  dot: RowDot;
  sub: string;
  /** Линия «не в сети» — у строки есть «×» (убрать из списка). */
  offline?: boolean;
}

export interface ChannelRow {
  channelId: number;
  projectId: number | null;
  title: string;
  sub: string;
}

export interface ProjectGroup {
  /** `UNSORTED_PROJECT_ID` — «Неразобранное». */
  id: number;
  name: string;
  color: string | null;
  sessions: SessionRow[];
  agents: AgentRow[];
  channels: ChannelRow[];
  waitCount: number;
}

const CACHE_WARN_MS = 10 * 60_000;
const CACHE_WARN_TOKENS = 100_000;

function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const mm = String(Math.floor(total / 60)).padStart(2, '0');
  const ss = String(total % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

/** Строка сессии хаба. `pending` — её ждущие карточки (загруженные), иначе счётчик записи. */
export function sessionRow(session: HubSession, pending: SessionRequest[] | undefined, nowMs: number): SessionRow {
  const own = pending;
  const requests = own ?? [];
  const asking = requests.some((r) => r.kind === 'question');
  const interrupted = hasInterrupted(session);
  const waits = (own?.length ?? session.pending_requests) > 0 || session.wait_reason === 'heavy' || session.wait_reason === 'paused' || interrupted;
  let dot: RowDot = 'idle';
  let sub: string;
  if (waits) {
    dot = 'wait';
    sub = asking ? 'спрашивает' : interrupted ? INTERRUPTED_STATE : 'ждёт тебя';
  } else if (session.state === 'running') {
    dot = 'run';
    sub = 'работает';
  } else if (session.state === 'asleep') {
    sub = 'спит';
  } else if (hasLiveBg(session)) {
    dot = 'run';
    sub = 'ждёт агентов';
  } else {
    sub = 'ждёт входа';
  }
  // Ф48-1: второй статус — фоновые агенты (при «ждёт агентов» он уже в самом состоянии).
  const bg = bgChipText(session);
  if (bg !== null && sub !== 'ждёт агентов') sub = `${sub} · ${bg}`;
  let cacheWarn: string | null = null;
  const cache = session.cache;
  const context = session.usage?.context_tokens ?? cache?.context_tokens ?? 0;
  if (cache != null && context > CACHE_WARN_TOKENS) {
    const left = cache.expires_ts - nowMs;
    if (left > 0 && left < CACHE_WARN_MS) cacheWarn = `кэш ${clock(left)}`;
  }
  return {
    item: { kind: 'session', id: session.id },
    id: session.id,
    name: sessionName(session),
    dot,
    sub,
    auto: session.mode === 'auto',
    subdir: session.subdir ? session.subdir : null,
    model: session.model?.trim() ? session.model.trim() : null,
    engineModel: `${sessionEngineLabel(session.engine)} · ${modelChipText(session)}`,
    cacheWarn,
    machine: session.machine ?? null,
  };
}

/** Имя линии «роль N» (номер — со второго); без роли — «без роли N». */
export function offlineName(line: Pick<OfflineLine, 'role' | 'role_no'>): string {
  const no = line.role_no ?? 1;
  if (line.role !== null && line.role.trim() !== '') return no >= 2 ? `${line.role} ${no}` : line.role;
  return `без роли ${no}`;
}

/** Линия «не в сети» убрана, если для её ключа есть запись с `ended_ts` не меньше её собственного: подписалась и ушла
 * снова (`ended_ts` новее) — видна опять. */
export function isLineHidden(line: Pick<OfflineLine, 'key' | 'ended_ts'>, hidden: readonly HiddenLine[]): boolean {
  const entry = hidden.find((h) => h.key === line.key);
  return entry !== undefined && entry.ended_ts >= (line.ended_ts ?? 0);
}

/** Линии «не в сети» без убранных. */
export function visibleOfflineLines(lines: readonly OfflineLine[], hidden: readonly HiddenLine[]): OfflineLine[] {
  return lines.filter((l) => !isLineHidden(l, hidden));
}

/** Серая строка линии «не в сети» (внешняя сессия, подписка которой кончилась). */
export function offlineRow(line: OfflineLine): AgentRow {
  return {
    item: { kind: 'agent', key: line.key },
    key: line.key,
    name: offlineName(line),
    dot: 'idle',
    sub: 'не в сети',
    offline: true,
  };
}

/** Подпись строки сессии на телефоне: «модель · ручной/авто · состояние · кэш» (пустые части пропускаются). */
export function phoneSessionSub(row: Pick<SessionRow, 'model' | 'auto' | 'sub' | 'cacheWarn'> & { viaAgent?: boolean }): string {
  return [row.model, row.auto ? 'авто' : 'ручной', row.viaAgent ? 'канал' : null, row.sub, row.cacheWarn].filter((p): p is string => !!p).join(' · ');
}

/** Строка внешней (не хабовской) живой сессии: движок и «слушает»/«не слушает». */
export function agentRow(line: AgentLine): AgentRow {
  const sub = line.sub as ProjectSub;
  return {
    item: { kind: 'agent', key: line.key },
    key: line.key,
    name: line.name,
    dot: sub.listening ? 'run' : 'idle',
    sub: `${engineLabel(sub.engine)} · ${listeningLabel(sub)}`,
    machine: sub.machine ?? null,
  };
}

function ago(ts: number): string {
  return new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

export interface BoardListInput {
  projects: Project[];
  sessions: HubSession[];
  pending: Record<number, SessionRequest[]>;
  agentLines: AgentLine[];
  home: Pick<HomeData, 'react' | 'drafts'>;
  waiting: WaitItem[];
  hiddenProjects: number[];
  nowMs: number;
  /** Ф41-6б: линии «не в сети» (раздел 5 п.3). */
  offline?: OfflineLine[];
  /** Убранные линии «не в сети» (`hidden_lines` раскладки). */
  hiddenLines?: HiddenLine[];
}

/** Группы списка слева: настоящие проекты (рабочая папка, сессия или ждущая беседа) в порядке API, затем
 * «Неразобранное» (если там что-то ждёт). Скрытые проекты не входят. */
export function buildBoardList(input: BoardListInput): ProjectGroup[] {
  const { projects, hiddenProjects } = input;
  const groups = new Map<number, ProjectGroup>();
  const ensure = (id: number): ProjectGroup => {
    let group = groups.get(id);
    if (group === undefined) {
      const project = projects.find((p) => p.id === id);
      group = {
        id,
        name: id === UNSORTED_PROJECT_ID ? 'Неразобранное' : (project?.name ?? `проект ${id}`),
        color: project?.color ?? null,
        sessions: [],
        agents: [],
        channels: [],
        waitCount: 0,
      };
      groups.set(id, group);
    }
    return group;
  };
  for (const project of projects) {
    if (project.id > 0 && project.workdir) ensure(project.id);
  }
  for (const session of input.sessions) {
    if (session.closed_ts !== null) continue;
    ensure(session.project_id).sessions.push(sessionRow(session, input.pending[session.id], input.nowMs));
  }
  for (const line of input.agentLines) {
    if (line.sub === null || line.sub.held || line.sub.live === false) continue;
    ensure(line.projectId).agents.push(agentRow(line));
  }
  const shown = new Set(input.agentLines.filter((l) => l.sub !== null && !l.sub.held && l.sub.live !== false).map((l) => l.key));
  for (const line of visibleOfflineLines(input.offline ?? [], input.hiddenLines ?? [])) {
    if (shown.has(line.key)) continue;
    ensure(line.project_id).agents.push(offlineRow(line));
  }
  const seen = new Set<number>();
  // Ф64-1: день проекта закрыт — его ждущие беседы не показываются (как «Ждёт тебя»).
  const closed = closedDayIds(projects);
  const addChannel = (projectId: number | null, row: ChannelRow): void => {
    if (projectId !== null && closed.has(projectId)) return;
    if (seen.has(row.channelId)) return;
    seen.add(row.channelId);
    ensure(projectId === null || projectId <= 0 ? UNSORTED_PROJECT_ID : projectId).channels.push(row);
  };
  for (const item of input.home.react) {
    addChannel(item.project.id, {
      channelId: item.channel.id,
      projectId: item.project.id,
      title: item.channel.title ?? '(без названия)',
      sub: item.draft_count > 0 ? 'черновик' : ago(item.last.ts),
    });
  }
  for (const draft of input.home.drafts) {
    addChannel(draft.project_id, {
      channelId: draft.channel_id,
      projectId: draft.project_id,
      title: draft.channel_title ?? '(без названия)',
      sub: `черновик #${draft.id}`,
    });
  }
  for (const item of input.waiting) {
    const id = item.project.id === null ? UNSORTED_PROJECT_ID : item.project.id;
    if (groups.has(id) || id === UNSORTED_PROJECT_ID) ensure(id).waitCount += 1;
  }
  const hidden = new Set(hiddenProjects);
  const rank = projectRank(projects);
  return [...groups.values()]
    .filter((g) => !hidden.has(g.id))
    .filter((g) => g.id !== UNSORTED_PROJECT_ID || g.channels.length > 0 || g.waitCount > 0)
    .sort((a, b) => {
      if (a.id === UNSORTED_PROJECT_ID) return 1;
      if (b.id === UNSORTED_PROJECT_ID) return -1;
      return rank(a.id) - rank(b.id);
    });
}

/** Скрытые проекты, о которых стоит сказать: имя для «Скрыты: …». */
export function hiddenNames(projects: Project[], hidden: number[]): { id: number; name: string }[] {
  return hidden.map((id) => ({
    id,
    name: id === UNSORTED_PROJECT_ID ? 'Неразобранное' : (projects.find((p) => p.id === id)?.name ?? `проект ${id}`),
  }));
}

export interface WaitBreakdown {
  id: number;
  name: string;
  count: number;
}

/** «Ждёт тебя · N»: N — число элементов `buildWaiting()` (то же, что бейдж вкладки «Ждёт» телефона); разбивка по
 * проектам — только с ненулевым числом, без скрытых. */
export function waitSummary(items: WaitItem[], hidden: number[], projects: Project[] = []): { total: number; byProject: WaitBreakdown[] } {
  const counts = new Map<number, WaitBreakdown>();
  for (const item of items) {
    const id = item.project.id === null ? UNSORTED_PROJECT_ID : item.project.id;
    const entry = counts.get(id) ?? { id, name: item.project.name, count: 0 };
    entry.count += 1;
    counts.set(id, entry);
  }
  const rank = projectRank(projects);
  const byProject = [...counts.values()]
    .filter((e) => !hidden.includes(e.id))
    .sort((a, b) => (projects.length > 0 ? rank(a.id) - rank(b.id) : 0));
  return { total: items.length, byProject };
}

/** Сессий в работе / всего: X — сессии хаба в `running`; Y — незакрытые сессии хаба и живые внешние. */
export function workSummary(sessions: HubSession[], agentLines: AgentLine[]): { running: number; total: number } {
  const open = sessions.filter((s) => s.closed_ts === null);
  const external = agentLines.filter((l) => l.sub !== null && !l.sub.held && l.sub.live !== false).length;
  return { running: open.filter((s) => s.state === 'running').length, total: open.length + external };
}
