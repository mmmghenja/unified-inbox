// Ф8-6 (`docs/architecture.md`): чистые функции главной страницы —
// начало дня, подписи поводов и статусов, дорожки «Claude по проектам». Без Vue, чтобы тестировать отдельно.
import type {
  AgentRun,
  AgentRunKind,
  AgentRunStatus,
  NewRecipient,
  OutboxListItem,
  Project,
} from '../api/types';

/** Полночь местного времени дня `nowMs` — «сегодня» главной. */
export function startOfDay(nowMs: number): number {
  const d = new Date(nowMs);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

export function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export const RUN_KIND_LABELS: Record<AgentRunKind, string> = {
  urgent: 'срочное',
  reply: 'реакция',
  digest: 'сводка',
  work: 'работа',
};

export const RUN_STATUS_LABELS: Record<AgentRunStatus, string> = {
  running: 'работает',
  waiting: 'ждёт тебя',
  ok: 'готово',
  failed: 'отказ',
  paused: 'пауза',
  retry: 'повтор',
  stopped: 'остановлена',
};

export function runKindLabel(kind: string): string {
  return RUN_KIND_LABELS[kind as AgentRunKind] ?? kind;
}

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status as AgentRunStatus] ?? status;
}

/** «<проект> › <беседа>» для строк главной. */
export function addressLabel(projectName: string | null | undefined, channelTitle: string | null | undefined): string {
  return `${projectName ?? '(без проекта)'} › ${channelTitle ?? '(без названия)'}`;
}

/** Строка «на что ответ» черновика: «ответ на: <отправитель>: <превью>» или «в тред: <превью>»; `null` — ни то
 * ни другое. */
export function draftTargetLabel(item: OutboxListItem): string | null {
  // Ф12-1 C2: черновик новому адресату — «кому: @username (новая беседа)» / «кому: адрес · тема».
  const recipient = newRecipientLabel(item.new_to);
  if (recipient !== null) return `кому: ${recipient}`;
  if (item.reply_to) {
    const who = item.reply_to.sender ?? '(отправитель неизвестен)';
    return `ответ на: ${who}: ${item.reply_to.excerpt ?? '(сообщение не загружено)'}`;
  }
  if (item.thread) return `в тред: ${item.thread.excerpt ?? '(родитель не загружен)'}`;
  return null;
}

/** Ф14-2 D3: кто адресат беседы-заготовки — `@username` / «адрес · тема» / имя человека Telegram / имена
 * людей Slack через запятую; `null` — не заготовка или неизвестный вид (новое ядро — не падать). */
export function newRecipientWho(newTo: NewRecipient | null | undefined): string | null {
  if (!newTo) return null;
  switch (newTo.kind) {
    case 'tg_username':
      return `@${newTo.username}`;
    case 'email':
      return `${newTo.to} · ${newTo.subject}`;
    case 'tg_user':
      return newTo.name || (newTo.username ? `@${newTo.username}` : `id ${newTo.user_id}`);
    case 'tg_msg_author':
      return newTo.from_title ? `${newTo.name} · из группы «${newTo.from_title}»` : newTo.name;
    case 'tg_phone':
      return typeof newTo.phone === 'string' && newTo.phone !== '' ? newTo.phone : null;
    case 'slack_users':
    case 'mm_users': {
      const names = Array.isArray(newTo.names) && newTo.names.length > 0 ? newTo.names : newTo.users;
      return Array.isArray(names) && names.length > 0 ? names.join(', ') : null;
    }
    default:
      return null;
  }
}

/** Ф12-1: адресат беседы-заготовки словами; `null` — не заготовка (или неизвестный вид). Ф14-2 D3: у Telegram
 * по id и у Slack — «<имя> (новая беседа)», как у @username. */
export function newRecipientLabel(newTo: NewRecipient | null | undefined): string | null {
  const who = newRecipientWho(newTo);
  if (who === null || !newTo) return null;
  return newTo.kind === 'email' ? who : `${who} (новая беседа)`;
}

/** Автор черновика: «Claude · <проект>» / «ты» / имя токена (ядро считает сам; старое ядро — по `origin`). */
export function draftAuthor(item: OutboxListItem): string {
  if (item.author) return item.author;
  return item.origin === 'ui' ? 'ты' : 'агент';
}

/** Подпись отправленного: «вручную» (из UI) или «через черновик Claude» (любой черновик от API). */
export function sentVia(item: OutboxListItem): string {
  if (item.origin === 'ui') return 'вручную';
  const author = draftAuthor(item);
  return author.startsWith('Claude') ? 'через черновик Claude' : `через черновик: ${author}`;
}

export interface ClaudeLane {
  projectId: number;
  name: string | null;
  color: string | null;
  /** Запуск в работе (`running`) — самый свежий, если их несколько. */
  running: AgentRun | null;
  lastStartedTs: number;
  /** Запуски дня проекта, новые сверху. */
  runs: AgentRun[];
}

export interface ClaudeLanes {
  lanes: ClaudeLane[];
  /** Проекты с `workdir` без запусков сегодня — одной строкой «сегодня не будился». */
  idle: Project[];
}

/** П2: запуски дня — дорожками по проекту. Сначала дорожки с `running`, затем по времени последнего запуска;
 * внутри дорожки — новые сверху. */
export function buildLanes(runs: AgentRun[], projects: Project[]): ClaudeLanes {
  const byProject = new Map<number, ClaudeLane>();
  for (const run of runs) {
    let lane = byProject.get(run.project_id);
    if (lane === undefined) {
      const project = projects.find((p) => p.id === run.project_id);
      lane = {
        projectId: run.project_id,
        name: run.project_name ?? project?.name ?? null,
        color: run.project_color ?? project?.color ?? null,
        running: null,
        lastStartedTs: run.started_ts,
        runs: [],
      };
      byProject.set(run.project_id, lane);
    }
    lane.runs.push(run);
    lane.lastStartedTs = Math.max(lane.lastStartedTs, run.started_ts);
  }
  for (const lane of byProject.values()) {
    lane.runs.sort((a, b) => b.started_ts - a.started_ts || b.id - a.id);
    lane.running = lane.runs.find((r) => r.status === 'running') ?? null;
  }
  const lanes = [...byProject.values()].sort((a, b) => {
    const aRunning = a.running !== null ? 1 : 0;
    const bRunning = b.running !== null ? 1 : 0;
    if (aRunning !== bRunning) return bRunning - aRunning;
    return b.lastStartedTs - a.lastStartedTs;
  });
  const idle = projects.filter((p) => p.id > 0 && p.workdir && !byProject.has(p.id));
  return { lanes, idle };
}

function channelsLabel(run: AgentRun): string {
  const titles = run.channels.map((c) => c.title ?? '(без названия)');
  return titles.length > 0 ? titles.join(', ') : 'без бесед';
}

/** Строка состояния дорожки: «работает N мин: <повод>, <беседы>» или «свободна · последний запуск <время>». */
export function laneStatusLine(lane: ClaudeLane, nowMs: number): string {
  if (lane.running !== null) {
    const minutes = Math.max(0, Math.floor((nowMs - lane.running.started_ts) / 60_000));
    return `работает ${minutes} мин: ${runKindLabel(lane.running.kind)}, ${channelsLabel(lane.running)}`;
  }
  return `свободна · последний запуск ${formatTime(lane.lastStartedTs)}`;
}
