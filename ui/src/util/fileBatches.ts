// Ф11-3b C2 (`docs/architecture.md`: раскладка прикреплённого на пачки —
// одна пачка = один `send` = одно сообщение источнику. Лимиты те же, что проверяет ядро
// (`uploads.MAX_FILES_PER_MESSAGE`, `MESSAGE_MAX_BYTES`, `MAIL_MAX_BYTES`): ядро только проверяет, раскладывает UI.

const MIB = 1024 * 1024;
export const MAX_FILES_PER_MESSAGE = 10;
export const MESSAGE_MAX_BYTES = 50 * MIB;
/** П6: 17 МиБ в base64 с переносами — меньше 25 МБ, которые принимает Gmail. */
export const MAIL_MAX_BYTES = 17 * MIB;
export const MAIL_TOO_BIG = 'Вложения письма больше 17 МиБ — уберите часть.';

export interface BatchItem {
  size: number;
  /** Вид из ответа `POST /uploads`: `photo` | `document`. */
  kind: string;
}

export type BatchResult<T> = { batches: T[][] } | { error: string };

/** Ф14-2 D4: в беседу-заготовку ядро примет только одно сообщение до привязки к настоящей беседе. */
export const FIRST_MESSAGE_TOO_MANY = `Первым сообщением — до ${MAX_FILES_PER_MESSAGE} файлов.`;
export const FIRST_MESSAGE_TOO_BIG = 'Первым сообщением — до 50 МиБ файлов суммой.';
export const FIRST_MESSAGE_MIXED = 'Первым сообщением в Telegram — только картинки или только файлы.';

/** Пачки в порядке поля. Почта — всегда одна пачка (одно письмо): не помещается — ошибка до отправки.
 * Остальные — не больше 10 файлов и 50 МиБ суммой; у Telegram новая пачка начинается и при смене вида
 * (в одном альбоме — только картинки или только файлы). Ф14-2 D4: `single` — беседа-заготовка нового адресата:
 * всё одной пачкой, не помещается — ошибка до отправки (второе сообщение ядро не примет до привязки). */
export function splitFileBatches<T extends BatchItem>(
  items: readonly T[],
  source: string,
  opts: { single?: boolean } = {},
): BatchResult<T> {
  if (items.length === 0) return { batches: [] };
  if (source === 'mail') {
    const total = items.reduce((sum, item) => sum + item.size, 0);
    if (total > MAIL_MAX_BYTES || items.length > MAX_FILES_PER_MESSAGE) return { error: MAIL_TOO_BIG };
    return { batches: [[...items]] };
  }
  if (opts.single === true) {
    if (items.length > MAX_FILES_PER_MESSAGE) return { error: FIRST_MESSAGE_TOO_MANY };
    if (items.reduce((sum, item) => sum + item.size, 0) > MESSAGE_MAX_BYTES) return { error: FIRST_MESSAGE_TOO_BIG };
    if (source === 'telegram' && new Set(items.map((item) => item.kind)).size > 1) return { error: FIRST_MESSAGE_MIXED };
    return { batches: [[...items]] };
  }
  const batches: T[][] = [];
  let current: T[] = [];
  let currentBytes = 0;
  for (const item of items) {
    const kindChanged = source === 'telegram' && current.length > 0 && current[0]!.kind !== item.kind;
    if (
      current.length > 0 &&
      (current.length >= MAX_FILES_PER_MESSAGE || currentBytes + item.size > MESSAGE_MAX_BYTES || kindChanged)
    ) {
      batches.push(current);
      current = [];
      currentBytes = 0;
    }
    current.push(item);
    currentBytes += item.size;
  }
  batches.push(current);
  return { batches };
}
