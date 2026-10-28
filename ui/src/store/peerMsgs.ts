// Ф35-3: сообщения агентов и диалог владельца с агентом. Чистый редьюсер без Vue (как `reminders.ts`) и чистые помощники
// для вкладки «Claude»: ключ линии, диалог, счётчики, подписи статусов. Тексты, роли и имена — чужой ввод: сюда
// попадают только как строки, выводятся текстовой интерполяцией.
import type { PeerMsg, PeerSide, ProjectSub } from '../api/types';
import { batchLabel as subBatchLabel } from '../util/sessionSub';
import { sessionEngineLabel } from '../util/engineProfiles';

export interface PeerMsgState {
  items: Record<number, PeerMsg>;
  /** Агенты в сети (`GET /subs?live=1`, строки проектных подписок). */
  agentSubs: ProjectSub[];
  /** Есть ли ещё старые сообщения за границей загруженного (для «ещё»). */
  more: boolean;
  error: string | null;
}

export function createPeerMsgState(): PeerMsgState {
  return { items: {}, agentSubs: [], more: true, error: null };
}

/** Строка из `/sync`, REST или ответа на отправку: старший `seq` побеждает. */
export function applyPeerMsg(state: PeerMsgState, msg: PeerMsg): void {
  const prev = state.items[msg.id];
  if (prev !== undefined && prev.seq > msg.seq) return;
  state.items[msg.id] = msg;
}

/** Роль для сравнения: без регистра, `ё` = `е`, пробелы схлопнуты. */
export function roleKey(role: string): string {
  return role.trim().toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ');
}

interface LineSide {
  project_id: number;
  role: string | null;
  role_no: number | null;
  sub_id?: number | null;
  id?: number;
}

/** Ключ линии: с ролью в настоящем проекте — `p<проект>:<роль>:<номер>`; безрольный и проект 0 — `s<sub_id>`. */
export function lineKey(side: LineSide): string {
  const role = side.role === null ? '' : roleKey(side.role);
  if (side.project_id > 0 && role !== '') return `p${side.project_id}:${role}:${side.role_no ?? 1}`;
  return `s${side.sub_id ?? side.id ?? 0}`;
}

export function hasRoleLine(side: LineSide): boolean {
  return side.project_id > 0 && side.role !== null && roleKey(side.role) !== '';
}

/** Все сообщения по убыванию id (новые первыми). */
export function sortedMsgs(items: Record<number, PeerMsg>): PeerMsg[] {
  return Object.values(items).sort((a, b) => b.id - a.id);
}

/** Сторона сообщения принадлежит линии: тот же ключ или та же подписка (Ф36-2 B4: после смены роли на месте у
 *  старых сообщений другой ключ, а `sub_id` тот же). */
function onLine(side: PeerSide, key: string, subId: number | null | undefined): boolean {
  return lineKey(side) === key || (subId != null && side.sub_id === subId);
}

/** Диалог владельца с линией: её строки `from_user` к этой линии и `to_user` от неё; по возрастанию id. */
export function dialogWith(items: Record<number, PeerMsg>, key: string, subId?: number | null): PeerMsg[] {
  return Object.values(items)
    .filter((m) => (m.from_user && m.to !== null && onLine(m.to, key, subId)) || (m.to_user && m.from !== null && onLine(m.from, key, subId)))
    .sort((a, b) => a.id - b.id);
}

/** Переписка агентов между собой: ни одна из сторон не владелец. Новые первыми. */
export function agentTraffic(items: Record<number, PeerMsg>): PeerMsg[] {
  return sortedMsgs(items).filter((m) => !m.from_user && !m.to_user);
}

/** Непрочитанные ответы владельцу (всего или по линии). */
export function unreadReplies(items: Record<number, PeerMsg>, key?: string, subId?: number | null): PeerMsg[] {
  return Object.values(items).filter(
    (m) => m.to_user && m.status === 'pending' && (key === undefined || (m.from !== null && onLine(m.from, key, subId))),
  );
}

export function unreadRepliesCount(items: Record<number, PeerMsg>, key?: string, subId?: number | null): number {
  return unreadReplies(items, key, subId).length;
}

/** Подпись статуса сообщения. */
export function statusLabel(msg: PeerMsg): string {
  switch (msg.status) {
    case 'pending':
      return msg.signals > 0 ? 'доставлено, не прочитано' : 'ждёт';
    case 'read':
      return 'прочитано';
    case 'lost':
      return 'не доставлено — сессия завершилась';
    case 'expired':
      return 'просрочено';
    default:
      return String(msg.status);
  }
}

/** Агент между собой: `pending` у адресата дольше 20 минут — сколько минут не прочитано, иначе `null`. */
export const UNREAD_WARN_MS = 20 * 60_000;
export function unreadMinutes(msg: PeerMsg, nowMs: number): number | null {
  if (msg.status !== 'pending' || msg.from_user || msg.to_user) return null;
  const age = nowMs - msg.created_ts;
  return age > UNREAD_WARN_MS ? Math.floor(age / 60_000) : null;
}

/** Ф48-4: сообщение агента ждёт срока пачки адресата (ещё не сигналило, срок впереди) — срок в мс,
 * иначе `null`. */
export function batchUntil(msg: PeerMsg, nowMs: number): number | null {
  if (msg.status !== 'pending' || msg.signals > 0) return null;
  const ts = msg.batch_ts;
  return typeof ts === 'number' && ts > nowMs ? ts : null;
}

/** Ф48-4: «в пачке до 14:05» для строки переписки; пачки нет — пустая строка. */
export function batchLabel(msg: PeerMsg, nowMs: number): string {
  const ts = batchUntil(msg, nowMs);
  if (ts === null) return '';
  const time = new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  return `в пачке до ${time}`;
}

export const MODE_LABELS: Record<string, string> = {
  cards: 'карточки',
  messages: 'каждое сообщение',
  direct: 'только сообщения агентов',
};

/** Ф38-1: фильтр подписки по аккаунтам — «только аккаунты 1, 5» / «кроме аккаунтов 8»; фильтра нет или
 * значение не той формы — пустая строка (правило — как `core/sub_accounts.py`). */
export function accountsLabel(sub: Pick<ProjectSub, 'accounts'>): string {
  const spec = sub.accounts;
  if (!spec || typeof spec !== 'object' || Array.isArray(spec) || Object.keys(spec).length !== 1) return '';
  const isIds = (ids: unknown): ids is number[] =>
    Array.isArray(ids) && ids.length > 0 && ids.every((x) => Number.isInteger(x) && x >= 1);
  if (isIds(spec.only)) return `только аккаунты ${spec.only.join(', ')}`;
  if (isIds(spec.except)) return `кроме аккаунтов ${spec.except.join(', ')}`;
  return '';
}

export function modeLabel(sub: Pick<ProjectSub, 'mode' | 'msg_filter' | 'accounts' | 'batch_min'>): string {
  if (sub.mode === 'direct') return MODE_LABELS.direct;
  const accounts = accountsLabel(sub);
  // Ф67-1: «· пачкой раз в N мин» — действующий интервал пачки сигналов подписки.
  const batch = subBatchLabel(sub.batch_min);
  const tail = (batch ? ` · ${batch}` : '') + (accounts ? ` · ${accounts}` : '');
  if (sub.mode === 'messages' && sub.msg_filter === 'personal') return `лично мне${tail}`;
  return (MODE_LABELS[sub.mode] ?? String(sub.mode)) + tail;
}

/** Подпись движка агента; неизвестный — «Claude Code» (Ф74-1: из профиля движка). */
export function engineLabel(engine: string | undefined): string {
  return sessionEngineLabel(engine, 'Claude Code');
}

/** Ф41-2а (3.6): «слушает» / «не слушает»; у сессии хаба — её состояние (её живость — запись, не процесс). */
export function listeningLabel(sub: Pick<ProjectSub, 'listening' | 'held' | 'state'>): string {
  if (sub.held) {
    if (sub.state === 'running') return 'в работе';
    if (sub.state === 'idle') return 'ждёт входа';
    return 'спит';
  }
  return sub.listening ? 'слушает' : 'не слушает';
}

export interface AgentAddress {
  project_id: number;
  role?: string;
  number?: number;
  sub_id?: number;
}

/** Строка блока «Агенты»: живая подписка или «не в сети» линия, у которой есть диалог с владельцем. */
export interface AgentLine {
  key: string;
  projectId: number;
  name: string;
  hasRole: boolean;
  /** Адрес отправки; у привязанной линии (`s<sub_id>`) — с `sub_id`: реплика именно этой подписке. */
  address: AgentAddress;
  sub: ProjectSub | null;
  cwd: string | null;
  unread: number;
}

function displayName(role: string | null, roleNo: number | null, withNumber: boolean): string {
  if (role !== null && roleKey(role) !== '') {
    return withNumber || (roleNo ?? 1) >= 2 ? `${role} ${roleNo ?? 1}` : role;
  }
  return `без роли ${roleNo ?? ''}`.trim();
}

/** Имя стороны сообщения (для «от -> кому»): роль, номер если ≥ 2, «без роли N». */
export function sideName(side: PeerSide): string {
  return displayName(side.role, side.role_no, false);
}

/** Диалог с безрольным и с агентом вне проектов привязан к подписке, а номер линии может занять другая сессия —
 *  такой адрес несёт `sub_id`, и ядро не отдаст реплику владельца преемнику (409). */
function addressOf(sub: ProjectSub, roleLine: boolean): AgentAddress {
  const address: AgentAddress = { project_id: sub.project_id };
  if (sub.role !== null && roleKey(sub.role) !== '') {
    address.role = sub.role;
    address.number = sub.role_no ?? 1;
  } else if (sub.role_no !== null) {
    address.number = sub.role_no;
  }
  if (!roleLine) address.sub_id = sub.id;
  return address;
}

export function buildAgentLines(items: Record<number, PeerMsg>, subs: ProjectSub[]): AgentLine[] {
  const lines: AgentLine[] = [];
  const seen = new Set<string>();
  for (const sub of subs) {
    if (sub.live === false) continue;
    const key = lineKey(sub);
    if (seen.has(key)) continue;
    seen.add(key);
    const roleLine = hasRoleLine(sub);
    // номер показываем, если в проекте есть другая живая подписка той же роли
    const sameRole = roleLine
      ? subs.filter((o) => o.live !== false && o.project_id === sub.project_id && o.role !== null && roleKey(o.role) === roleKey(sub.role ?? '')).length > 1
      : false;
    lines.push({
      key,
      projectId: sub.project_id,
      name: displayName(sub.role, sub.role_no, sameRole),
      hasRole: roleLine,
      address: addressOf(sub, roleLine),
      sub,
      cwd: sub.project_id === 0 ? (sub.cwd ?? null) : null,
      unread: unreadRepliesCount(items, key, sub.id),
    });
  }
  // Подписки, показанные в списке: сообщения с их `sub_id` — их диалог, а не «призрак» под прежним ключом (Ф36-2 B4).
  const liveIds = new Set(lines.map((l) => l.sub?.id).filter((id): id is number => typeof id === 'number'));
  // Линии с ролью, у которых есть диалог с владельцем, но нет живой подписки.
  const offline = new Map<string, PeerSide>();
  for (const m of Object.values(items)) {
    const side = m.from_user ? m.to : m.to_user ? m.from : null;
    if (side === null || !hasRoleLine(side)) continue;
    if (side.sub_id != null && liveIds.has(side.sub_id)) continue;
    const key = lineKey(side);
    if (!seen.has(key) && !offline.has(key)) offline.set(key, side);
  }
  for (const [key, side] of offline) {
    lines.push({
      key,
      projectId: side.project_id,
      name: displayName(side.role, side.role_no, false),
      hasRole: true,
      address: { project_id: side.project_id, role: side.role ?? '', number: side.role_no ?? 1 },
      sub: null,
      cwd: null,
      unread: unreadRepliesCount(items, key),
    });
  }
  return lines;
}
