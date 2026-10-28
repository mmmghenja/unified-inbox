// Группировка ленты в строки показа (спека 1d-1, п.2 «Альбом»): подряд идущие (в порядке показа) сообщения
// ОДНОГО отправителя с одинаковым `grouped_id` схлопываются в одну строку. Чистая функция, без Vue (тот же
// принцип, что у `applySync.ts`/`visibleMessage.ts`) — вызывающая сторона (`MessageFeed.vue`) оборачивает её
// результат в `computed()`, и правка/удаление ЛЮБОГО сообщения альбома обновляет строку сама — `messages`
// приходит уже из реактивного стора, здесь достаточно просто пересчитать группировку на новом массиве.
import type { Message } from '../api/types';

export type FeedRow =
  | { kind: 'single'; message: Message }
  | { kind: 'album'; groupedId: string; messages: Message[] };

function sameSender(a: Message, b: Message): boolean {
  if (a.is_me !== b.is_me) return false;
  if (a.is_me) return true; // оба «я» (исходящие свои сообщения) — считаем тем же отправителем
  return (a.sender?.id ?? null) === (b.sender?.id ?? null);
}

/** `messages` — уже отсортирован вызывающей стороной в порядке показа `(ts, ext_sort, id)`
 * (`sync.messagesByChannel`, см. `applySync.compareMessages`). */
export function buildFeedRows(messages: readonly Message[]): FeedRow[] {
  const rows: FeedRow[] = [];

  for (const message of messages) {
    const last = rows[rows.length - 1];

    if (message.grouped_id !== null && last !== undefined) {
      if (last.kind === 'album' && last.groupedId === message.grouped_id && sameSender(last.messages[0]!, message)) {
        last.messages.push(message);
        continue;
      }
      if (
        last.kind === 'single' &&
        last.message.grouped_id === message.grouped_id &&
        sameSender(last.message, message)
      ) {
        rows[rows.length - 1] = {
          kind: 'album',
          groupedId: message.grouped_id,
          messages: [last.message, message],
        };
        continue;
      }
    }

    rows.push({ kind: 'single', message });
  }

  return rows;
}
