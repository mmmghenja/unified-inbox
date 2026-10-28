// Ф17-1b (раздел D): что из заявок показывать карточкой и как называть исход простыми словами. Чистые функции.
// Всё из предпросмотра (название, описание) и причина сессии — недоверенный текст: показывается только строками.
import type { JoinRequest } from '../api/types';

/** Исход `failed` / `requested` виден сутки после решения владельца. */
export const JOIN_RESULT_TTL_MS = 24 * 60 * 60 * 1000;

const FAIL_REASONS: Record<string, string> = {
  not_found: 'такого канала или группы нет',
  not_group: 'это человек или бот',
  not_channel: 'это не канал и не группа',
  invite_expired: 'приглашение истекло',
  invite_invalid: 'приглашение неверное',
  private: 'закрытый, вступить нельзя',
  paid: 'платный канал',
  too_many_channels: 'Telegram: слишком много групп и каналов у аккаунта',
  group_full: 'группа заполнена',
  gave_up: 'Telegram не отвечает, попробуйте позже',
};

export function failReason(error: string | null): string {
  if (error === null || error === '') return 'не получилось';
  return FAIL_REASONS[error] ?? error;
}

/** Карточка нужна: ждёт решения, уже одобрена (вступаю), либо исход `failed`/`requested` не старше суток. */
export function joinVisible(join: JoinRequest, nowMs: number): boolean {
  if (join.status === 'pending' || join.status === 'approved') return true;
  if (join.status === 'failed' || join.status === 'requested') {
    return join.decided_ts !== null && nowMs - join.decided_ts <= JOIN_RESULT_TTL_MS;
  }
  return false;
}

/** Ждёт владельца только `pending`: одобренная («вступаю»), «заявка у админов» и неудача его решения не ждут —
 * в «Ждёт тебя» их нет, они видны короткой строкой в столбце проекта. */
export function waitingJoins(joins: JoinRequest[]): JoinRequest[] {
  return joins.filter((j) => j.status === 'pending').sort((a, b) => a.id - b.id);
}

export function visibleJoins(joins: JoinRequest[], nowMs: number): JoinRequest[] {
  return joins.filter((j) => joinVisible(j, nowMs)).sort((a, b) => a.id - b.id);
}

/** «12 400» — с неразрывными пробелами тысяч. */
function count(n: number): string {
  return n.toLocaleString('ru-RU');
}

function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

/** «канал · 12 400 подписчиков» / «группа · 85 участников»; вид неизвестен — только число или пусто. */
export function joinKindLine(join: JoinRequest): string {
  const peek = join.peek;
  if (peek === null) return '';
  const n = peek.participants;
  if (peek.kind === 'broadcast') {
    return n === null ? 'канал' : `канал · ${count(n)} ${plural(n, 'подписчик', 'подписчика', 'подписчиков')}`;
  }
  if (peek.kind === 'megagroup' || peek.kind === 'group') {
    const what = peek.forum ? 'форум' : 'группа';
    return n === null ? what : `${what} · ${count(n)} ${plural(n, 'участник', 'участника', 'участников')}`;
  }
  return '';
}

/** Название: предпросмотр, иначе `@имя` цели, иначе «по приглашению». */
export function joinTitle(join: JoinRequest): string {
  const title = join.peek?.title ?? join.title;
  if (title !== null && title !== undefined && title.trim() !== '') return title;
  if (join.target_kind === 'username') {
    return join.target.startsWith('@') ? join.target : `@${join.target}`;
  }
  return 'по приглашению';
}
