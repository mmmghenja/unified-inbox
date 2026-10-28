// Ф11-7 (`docs/architecture.md`: отметка у МОИХ отправленных сообщений Telegram —
// «✓» (доставлено) или «✓✓» (собеседник прочитал: позиция сообщения не дальше `peer_read_sort` беседы; в группе
// — «прочитал кто-то»). Заготовки и неотправленное — без отметки (у них свой статус outbox). Slack и почта
// прочтение собеседником не отдают — без отметок. Беседа не Telegram, но с маркером (fake-источник стенда) —
// тоже с отметками. Ленты (`channel`) и темы форумов (`topic`) — нет: прочтение там не приходит.
import type { Channel, Message } from '../api/types';

export type PeerReadMark = 'sent' | 'read';

const PEER_READ_KINDS = new Set(['dm', 'group']);
// Числовой `ext_id` Telegram (как `extsort.ext_sort` ядра); длиннее 15 цифр в `number` уже не точно.
const NUMERIC_EXT_ID = /^\d{1,15}$/;

export function peerReadMark(message: Message, channel: Channel | undefined): PeerReadMark | null {
  if (channel === undefined || !PEER_READ_KINDS.has(channel.kind)) return null;
  const marker = channel.peer_read_sort ?? null;
  if (channel.source !== 'telegram' && channel.source !== 'whatsapp' && marker === null) return null;
  if (!message.is_me || message.kind === 'event' || message.deleted || message.hidden) return null;
  if (message.outbox_status !== null && message.outbox_status !== 'sent') return null;
  if (channel.source === 'whatsapp') {
    // Ф62-1: id WhatsApp не число — маркер ядра хранит время (мс) прочитанного сообщения.
    if (message.ext_id === null) return null;
    return marker !== null && message.ts <= marker ? 'read' : 'sent';
  }
  if (message.ext_id === null || !NUMERIC_EXT_ID.test(message.ext_id)) return null;
  return marker !== null && Number(message.ext_id) <= marker ? 'read' : 'sent';
}

export function peerReadTitle(mark: PeerReadMark, channel: Channel | undefined): string {
  if (mark === 'sent') return 'Доставлено, ещё не прочитано';
  return channel?.kind === 'group' ? 'Прочитано (кем-то из участников)' : 'Прочитано';
}
