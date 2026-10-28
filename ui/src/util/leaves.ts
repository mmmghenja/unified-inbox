// Ф17-2b (раздел D): выход из групп и каналов Telegram — что показывать карточкой, состояние кнопки в шапке
// беседы, причины простыми словами. Чистые функции. Название и причина — чужой текст: только строками.
import type { Channel, LeaveRequest } from '../api/types';

/** Ядро отдаёт закрытые заявки за 7 дней; `failed` в шапке виден столько же. */
export const LEAVE_FAILED_TTL_MS = 7 * 24 * 60 * 60 * 1000;

const FAIL_REASONS: Record<string, string> = {
  creator: 'вы создатель, Telegram не выпускает создателя канала',
  not_found: 'беседы уже нет в Telegram',
  forbidden: 'Telegram не дал выйти',
  gave_up: 'Telegram не ответил, попробуйте ещё раз',
};

export function leaveFailReason(error: string | null): string {
  if (error === null || error === '') return 'не получилось';
  return FAIL_REASONS[error] ?? error;
}

/** Карточка в столбце проекта и в «Ждёт»: ждёт решения или уже одобрена (выхожу). */
export function leaveVisible(leave: LeaveRequest): boolean {
  return leave.status === 'pending' || leave.status === 'approved';
}

export function visibleLeaves(leaves: LeaveRequest[]): LeaveRequest[] {
  return leaves.filter(leaveVisible).sort((a, b) => a.id - b.id);
}

export function leaveTitle(leave: LeaveRequest): string {
  const title = leave.title;
  if (title !== null && title.trim() !== '') return title;
  return `беседа #${leave.channel_id}`;
}

export function leaveKindLabel(leave: LeaveRequest): string {
  return leave.chat_kind === 'channel' ? 'канал' : 'группа';
}

/** Кнопка шапки: только беседы Telegram — группа, канал, тема форума; архивные — без кнопки. */
export function canLeaveChannel(channel: Channel | null): boolean {
  if (channel === null || channel.archived) return false;
  if (channel.source === 'whatsapp') return channel.kind === 'group';
  return channel.source === 'telegram' && (channel.kind === 'group' || channel.kind === 'channel' || channel.kind === 'topic');
}

export function leaveButtonLabel(kind: Channel['kind']): string {
  if (kind === 'channel') return 'Выйти из канала';
  if (kind === 'topic') return 'Выйти из форума';
  return 'Выйти из группы';
}

export function leaveConfirmText(title: string | null, source: string = 'telegram'): string {
  const where = source === 'whatsapp' ? 'в WhatsApp' : 'в Telegram';
  return `Выйти из «${title ?? ''}» ${where}? Беседа уйдёт в архив хаба, история сохранится`;
}

export const LEFT_NOTICE = 'Вы вышли, беседа в архиве хаба';

export interface LeaveButtonState {
  /** Выход идёт (`approved`) — «Выхожу…», кнопка неактивна. */
  leaving: boolean;
  /** Последняя заявка `failed` за 7 дней — причина для строки рядом. */
  failure: string | null;
  /** Последняя заявка — `left` и выход в силе (хаб беседу не возвращал, `returned_ts` пусто); вместе с
   *  `archived` беседы даёт строку «Вы вышли». Архив после возврата — архив владельца, не выход. */
  left: boolean;
}

/** Состояние по заявкам беседы (у темы — её форума): решает самая новая по id. */
export function leaveButtonState(leaves: LeaveRequest[], nowMs: number): LeaveButtonState {
  const latest = leaves.reduce<LeaveRequest | null>((acc, l) => (acc === null || l.id > acc.id ? l : acc), null);
  if (latest === null) return { leaving: false, failure: null, left: false };
  if (latest.status === 'approved') return { leaving: true, failure: null, left: false };
  if (latest.status === 'failed' && nowMs - latest.updated_ts <= LEAVE_FAILED_TTL_MS) {
    return { leaving: false, failure: leaveFailReason(latest.error), left: false };
  }
  return { leaving: false, failure: null, left: latest.status === 'left' && latest.returned_ts === null };
}
