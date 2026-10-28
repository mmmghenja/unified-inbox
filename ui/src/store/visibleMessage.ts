// F5 (приёмка 6b, дефект): «нижнее видимое сообщение» раньше искалось как `max(id)` среди видимых —
// неверно, потому что порядок показа `(ts, ext_sort, id)` не совпадает с монотонностью `id` (сверка/догрузка
// истории Ф1 может вставить строку со старым `ts`, но бóльшим внутренним `id`, чем у уже показанных выше).
// Чистая функция — без DOM, тестируется напрямую (`tests/13-visible-message.spec.ts`).
import type { Message } from '../api/types';
import { compareMessages } from './applySync';

/** `messages` — уже отсортированный по порядку показа массив (как в `sync.messagesByChannel`). Ищем
 * последнее (самое нижнее в ленте) сообщение, чей `id` есть среди `visibleIds`, сканируя с конца — так
 * позиция в массиве, а не величина `id`, определяет «нижнее». */
export function findBottomVisibleMessage(
  messages: readonly Message[],
  visibleIds: ReadonlySet<number>,
): Message | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message !== undefined && visibleIds.has(message.id)) {
      return message;
    }
  }
  return null;
}

/** `true`, если `candidate` строго ПОЗЖЕ по порядку показа, чем `previous` (или `previous` ещё не было) —
 * ровно то, что нужно, чтобы не слать `read` назад при перерисовке/повторном срабатывании наблюдателя. */
export function isNewerInDisplayOrder(candidate: Message, previous: Message | null): boolean {
  return previous === null || compareMessages(candidate, previous) > 0;
}
