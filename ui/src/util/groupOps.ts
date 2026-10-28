// Ф62-6 (раздел G): группы WhatsApp — что показывать карточкой, тексты, исход по участникам, разбор участников
// из поиска «Кому». Чистые функции. Название, причина и имена — чужой текст: только строками.
import type { Channel, GroupOp, GroupOpCode, GroupOpParticipant, RecipientHit } from '../api/types';

/** Выбранный участник: что слать ядру и что показывать. */
export interface PickedPerson {
  value: string;
  label: string;
}

/** Название группы WhatsApp — не длиннее (длиннее сервер отвечает 406). */
export const GROUP_TITLE_MAX = 25;
/** Участников в одной заявке — предел ручки. */
export const GROUP_MEMBERS_MAX = 30;

const CODE_TEXT: Record<GroupOpCode, string> = {
  ok: 'готово',
  privacy: 'приватность — пошли ссылку',
  already: 'уже в группе',
  not_member: 'не в группе',
  no_whatsapp: 'нет WhatsApp',
  no_pn: 'нет номера',
  self: 'это ты',
  left_recently: 'недавно вышел',
  error: 'не получилось',
  bad: 'неверный номер',
};

const ERROR_TEXT: Record<string, string> = {
  gave_up: 'WhatsApp не ответил, попробуйте ещё раз',
  not_admin: 'нужны права админа группы',
  not_member: 'вы не в этой группе',
  not_found: 'группы уже нет',
  rejected: 'WhatsApp отказал',
  no_participants: 'некого добавлять: ни у кого нет WhatsApp',
  unsupported: 'мост WhatsApp этого не умеет',
};

export function groupOpErrorText(error: string | null): string {
  if (error === null || error === '') return 'не получилось';
  return ERROR_TEXT[error] ?? error;
}

export function groupOpCodeText(code: GroupOpCode | undefined): string {
  return code === undefined ? '' : (CODE_TEXT[code] ?? code);
}

/** Карточка в столбце проекта и «Ждёт»: ждёт решения или уже одобрена (выполняется). */
export function groupOpVisible(op: GroupOp): boolean {
  return op.status === 'pending' || op.status === 'approved';
}

/** Только заявки сессий: заявка владельца из интерфейса идёт сразу `approved` и карточки не просит. Ждущая решения
 * заявка владельца бывает только после восстановления из копии (Ф33-3: ядро вернуло одобренную на решение) — карточкой. */
export function visibleGroupOps(ops: GroupOp[]): GroupOp[] {
  return ops
    .filter(
      (op) =>
        groupOpVisible(op) &&
        (op.origin !== 'ui' || op.status === 'pending') &&
        op.kind !== 'link' &&
        op.kind !== 'members',
    )
    .sort((a, b) => a.id - b.id);
}

/** Имя участника для строки: имя и маска, иначе что есть. */
export function participantLabel(p: GroupOpParticipant): string {
  const name = p.name?.trim() ?? '';
  if (name !== '' && p.mask !== '' && name !== p.mask) return `${name} (${p.mask})`;
  return name || p.mask || `участник ${p.i}`;
}

export function participantsLine(op: GroupOp): string {
  return op.participants.map(participantLabel).join(', ');
}

function quoted(title: string): string {
  return `«${title}»`;
}

/** Название беседы для заявок на уже существующую группу: из заявки, иначе из беседы, иначе по номеру. */
export function groupOpTarget(op: GroupOp, channelTitle?: string | null): string {
  const t = op.title?.trim() || channelTitle?.trim() || '';
  return t !== '' ? quoted(t) : `беседа #${op.channel_id ?? '?'}`;
}

/** Текст вопроса карточки: «Создать группу «…» с: …?» и т. д. */
export function groupOpQuestion(op: GroupOp, channelTitle?: string | null): string {
  const who = participantsLine(op);
  switch (op.kind) {
    case 'create':
      return `Создать группу ${quoted(op.title?.trim() ?? '')}${who !== '' ? ` с: ${who}` : ''}?`;
    case 'add':
      return `Добавить в ${groupOpTarget(op, channelTitle)}: ${who}?`;
    case 'remove':
      return `Убрать из ${groupOpTarget(op, channelTitle)}: ${who}?`;
    case 'link_reset':
      return `Сбросить ссылку ${groupOpTarget(op, channelTitle)}?`;
    case 'members':
      return 'Состав группы';
    default:
      return `Ссылка-приглашение ${groupOpTarget(op, channelTitle)}`;
  }
}

/** Итог по участникам после выполнения: «Имя — приватность — пошли ссылку». Пусто, пока кодов нет. */
export function groupOpOutcomeLines(op: GroupOp): string[] {
  return op.participants
    .filter((p) => p.code !== undefined)
    .map((p) => `${participantLabel(p)} — ${groupOpCodeText(p.code)}`);
}

/** Одной строкой для заявки в процессе. */
export function groupOpProgress(op: GroupOp): string {
  switch (op.kind) {
    case 'create':
      return 'создаётся…';
    case 'add':
      return 'добавляю…';
    case 'remove':
      return 'убираю…';
    case 'link_reset':
      return 'сбрасываю ссылку…';
    case 'members':
      return 'загружаю состав…';
    default:
      return 'получаю ссылку…';
  }
}

/** Заявка закрыта: больше не изменится. */
export function groupOpClosed(op: GroupOp): boolean {
  return op.status === 'done' || op.status === 'failed' || op.status === 'declined';
}

/** Меню шапки: беседа-группа WhatsApp, не архивная. */
export function isWhatsappGroup(channel: Channel | null): boolean {
  return channel !== null && !channel.archived && channel.source === 'whatsapp' && channel.kind === 'group';
}

/** Участник заявки из строки поиска «Кому»: лички WhatsApp — `#id`, человек — его номер или jid. Не WhatsApp — null. */
export function participantOfHit(hit: RecipientHit, accountId: number): { value: string; label: string } | null {
  if (hit.source !== 'whatsapp' || hit.account_id !== accountId) return null;
  if (hit.type === 'channel') {
    if (hit.kind === 'group' || hit.kind === 'channel') return null;
    return { value: `#${hit.channel_id}`, label: hit.title ?? `#${hit.channel_id}` };
  }
  const name = hit.name?.trim() || hit.handle?.trim() || '';
  if (hit.channel_id !== null) return { value: `#${hit.channel_id}`, label: name || `#${hit.channel_id}` };
  if (typeof hit.to === 'string' && hit.to !== '') return { value: hit.to, label: name || hit.to };
  return null;
}

/** Номер, набранный руками: `+7…` или `8…`, 8–15 цифр. Иначе null. */
export function manualPhone(input: string): string | null {
  const value = input.trim();
  if (!/^\+?\d[\d\s().-]{6,}$/.test(value)) return null;
  const digits = value.replace(/\D/g, '');
  if (digits.length < 8 || digits.length > 15) return null;
  if (!value.startsWith('+') && !value.startsWith('8')) return null;
  return value;
}
