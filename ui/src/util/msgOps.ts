// Ф14-8 (`docs/architecture.md`): правка и «удалить у всех» своих
// сообщений Telegram/Slack. Здесь — одно место для условий кнопок (те же правила, что проверяет ядро
// `core/msg_ops.py`: `_load` + `_check_editable`), предупреждения про entities и текстов причин неудачи.
import type { Channel, Message, MessageOp } from '../api/types';

/** Источники, где ядро правит и удаляет у всех (`core/msg_ops.SOURCES`). */
const OP_SOURCES: ReadonlySet<string> = new Set(['telegram', 'slack', 'mattermost', 'fake', 'whatsapp']);
/** П6: подпись Telegram правится, только если ВСЕ вложения из этого списка (кружок — `video` с `round` — нет). */
const TELEGRAM_EDITABLE_FILES: ReadonlySet<string> = new Set([
  'photo',
  'video',
  'document',
  'audio',
  'voice',
  'animation',
]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function rawList(json: string | null | undefined): unknown[] {
  if (!json) return [];
  let value: unknown;
  try {
    value = JSON.parse(json);
  } catch {
    return [];
  }
  if (isPlainObject(value)) return [value];
  return Array.isArray(value) ? value : [];
}

/** П6 — копия `core/msg_ops.telegram_file_editable`. */
export function telegramFileEditable(attachmentsJson: string | null): boolean {
  const items = rawList(attachmentsJson);
  if (items.length === 0) return false;
  return items.every(
    (item) =>
      isPlainObject(item) &&
      typeof item.type === 'string' &&
      TELEGRAM_EDITABLE_FILES.has(item.type) &&
      item.round !== true,
  );
}

/** Общее для правки и удаления у всех: своё сообщение источника с правкой, не служебное, не удалённое, не
 * скрытое, не в беседе-заготовке. */
function ownSourceMessage(message: Message, channel: Channel | undefined): boolean {
  if (channel?.source === undefined || !OP_SOURCES.has(channel.source)) return false;
  if ((channel.ext_id ?? '').startsWith('new:')) return false;
  return (
    message.is_me &&
    message.ext_id !== null &&
    message.kind !== 'event' &&
    !message.deleted &&
    message.hidden !== true
  );
}

/** «Изменить»: текст или файл, не пересланное, нет ждущего удаления; Telegram-файл — по П6. */
export function canEdit(message: Message, channel: Channel | undefined): boolean {
  if (!ownSourceMessage(message, channel)) return false;
  if (message.kind !== 'text' && message.kind !== 'file') return false;
  if (message.forward_json) return false;
  if (message.op?.kind === 'delete' && message.op.state === 'queued') return false;
  if (channel?.source === 'telegram' && message.kind === 'file') return telegramFileEditable(message.attachments_json);
  // Ф62-1: WhatsApp — правится только текст (как `core/msg_ops._check_editable`).
  if (channel?.source === 'whatsapp') return message.kind === 'text' && rawList(message.attachments_json).length === 0;
  return true;
}

/** «Удалить у всех». */
export function canRevoke(message: Message, channel: Channel | undefined): boolean {
  return ownSourceMessage(message, channel);
}

export const REVOKE_CONFIRM = 'Удалить сообщение у всех? Оно пропадёт у собеседника в Telegram/Slack/Mattermost/WhatsApp. Отменить нельзя.';
export const ENTITIES_LOST_WARNING = 'Форматирование, ссылки под текстом и упоминания станут обычным текстом';

/** Раздел D: пропадут ли при правке entities (правка уходит простым текстом). Telegram — любые, кроме `link`,
 * чей видимый текст равен `url`, и `mention`, чей текст начинается с «@»; Slack — любые, кроме такой же `link`.
 * Разбирается сырой список (не `parseEntities`: тот отбрасывает незнакомые типы, а они тоже пропадут). */
export function editLosesEntities(source: string | undefined, text: string | null, entitiesJson: string | null): boolean {
  const body = text ?? '';
  for (const item of rawList(entitiesJson)) {
    if (!isPlainObject(item) || typeof item.type !== 'string') continue;
    const offset = typeof item.offset === 'number' ? item.offset : 0;
    const length = typeof item.length === 'number' ? item.length : 0;
    const visible = body.slice(offset, offset + length);
    if (item.type === 'link' && typeof item.url === 'string' && visible === item.url) continue;
    if (source === 'telegram' && item.type === 'mention' && visible.startsWith('@')) continue;
    return true;
  }
  return false;
}

/** Раздел D: причина неудачи по коду ядра. */
export function opFailureReason(op: MessageOp): string {
  const code = op.error?.code ?? 'other';
  switch (code) {
    case 'expired':
      return 'истёк срок правки у источника (Telegram — 48 часов)';
    case 'forbidden':
      return 'источник не разрешает';
    case 'not_found':
      return 'сообщения больше нет у источника';
    case 'too_long':
      return 'текст длиннее, чем разрешает источник';
    default:
      return `не получилось (${op.error?.detail ?? code})`;
  }
}

/** Текст ошибки ручки правки/удаления/отмены для строки под полем или сообщения. */
export function opRequestErrorText(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'status' in err) {
    const status = (err as { status: unknown }).status;
    const message = err instanceof Error ? err.message : '';
    if (status === 409) {
      if (message === 'in_flight') return 'уже отправляется';
      if (message === 'delete_pending') return 'сообщение удаляется у всех';
      if (message === 'not_editable' || message === 'unsupported') return 'это сообщение изменить нельзя';
      if (message === 'op_pending') return 'дождитесь итога правки или удаления';
    }
    if (status === 422) {
      if (message === 'empty') return 'пустой текст';
      if (message.startsWith('too_long')) return `текст слишком длинный${message.slice('too_long'.length)}`;
    }
    if (status === 404) return 'сообщение не найдено';
  }
  return 'не получилось — попробуйте ещё раз';
}
