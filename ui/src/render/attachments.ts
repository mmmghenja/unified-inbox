// Разбор `attachments_json` (спека 1d-1, п.1). Формат — JSON-список объектов
// `{ type, name?, size?, mime?, ttl?, external?, gone? }`, `type` ∈ фиксированный набор. Вход недоверенный
// (пришёл от собеседника/коннектора): любое отклонение отдельного элемента (неизвестный `type`, лишние/неверные
// поля) — этот элемент просто отбрасывается, остальные валидные показываются; битый JSON/не-список — пустой
// список, ничего не падает. `mime` в отображение не входит (спека требует только тип/имя/размер).
//
// Ф8-5: `idx` — позиция вложения в ИСХОДНОМ списке (по ней ядро связывает вложение со строкой `media` и ручкой
// `…/media/{idx}/fetch`, отброшенные соседи её не сдвигают); флаги `ttl`/`external`/`gone` — надписи вместо
// «Загрузить» (контракт 5b). Ссылки «открыть в Telegram» (`source_url`, `t.me`) убраны (B5, план 11.6).

export type AttachmentType =
  | 'photo'
  | 'video'
  | 'voice'
  | 'audio'
  | 'document'
  | 'sticker'
  | 'gif'
  | 'location'
  | 'contact'
  | 'poll'
  | 'other';

const TYPE_LABELS: Record<AttachmentType, string> = {
  photo: 'Фото',
  video: 'Видео',
  voice: 'Голосовое',
  audio: 'Аудио',
  document: 'Документ',
  sticker: 'Стикер',
  gif: 'GIF',
  location: 'Геопозиция',
  contact: 'Контакт',
  poll: 'Опрос',
  other: 'Файл',
};

const KNOWN_TYPES: ReadonlySet<string> = new Set(Object.keys(TYPE_LABELS));

export interface ParsedAttachment {
  idx: number;
  type: AttachmentType;
  name: string | null;
  size: number | null;
  /** Одноразовое фото/видео Telegram — не качается (П13). */
  ttl: boolean;
  /** Внешний файл Slack — не качается (П2). */
  external: boolean;
  /** Файл удалён у источника (tombstone Slack, П10). */
  gone: boolean;
  /** Ф14-6 D: видео-кружок Telegram (`round`) и аудиоклип Slack (`clip`) — расшифровываются. */
  round?: boolean;
  clip?: boolean;
  /** Стикер Telegram: `animated` (.tgs, Lottie), `video` (.webm), `static` (.webp) и его эмодзи. Нет у старых. */
  stickerFormat?: StickerFormat;
  emoji?: string;
}

export type StickerFormat = 'animated' | 'video' | 'static';
const STICKER_FORMATS: ReadonlySet<string> = new Set(['animated', 'video', 'static']);

const PHOTO_MIMES: ReadonlySet<string> = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif']);

/** Ф11-3a A1: вид вложения по `mime` — то же правило, что у ядра (`media.mail_attachment_kind`): растровая
 * картинка — `photo`, иначе `document`. Нужен вложениям без `type` (письма, пришедшие до Ф11-3a). */
export function kindFromMime(mime: string): AttachmentType {
  const base = mime.split(';')[0]!.trim().toLowerCase();
  return PHOTO_MIMES.has(base) ? 'photo' : 'document';
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Разобрать и провалидировать `attachments_json`. Возвращает только валидные вложения, в исходном порядке. */
export function parseAttachments(attachmentsJson: string | null): ParsedAttachment[] {
  if (!attachmentsJson) return [];
  let raw: unknown;
  try {
    raw = JSON.parse(attachmentsJson);
  } catch {
    return [];
  }
  if (!Array.isArray(raw)) return [];

  const result: ParsedAttachment[] = [];
  for (const [idx, item] of raw.entries()) {
    if (!isPlainObject(item)) continue;
    // Ф11-3a A1: без `type`, но с `mime` (старое письмо) — вид по `mime`; без обоих — отбрасывается, как было.
    const type = !('type' in item) && typeof item.mime === 'string' ? kindFromMime(item.mime) : item.type;
    if (typeof type !== 'string' || !KNOWN_TYPES.has(type)) continue;
    const name = typeof item.name === 'string' && item.name.length > 0 ? item.name : null;
    const size =
      typeof item.size === 'number' && Number.isFinite(item.size) && item.size >= 0 ? item.size : null;
    result.push({
      idx,
      type: type as AttachmentType,
      name,
      size,
      ttl: item.ttl === true,
      external: item.external === true,
      gone: item.gone === true,
      // Ключи — только когда флаг есть: прежние вложения разбираются в тот же объект.
      ...(item.round === true ? { round: true } : {}),
      ...(item.clip === true ? { clip: true } : {}),
      ...(type === 'sticker' && typeof item.sticker_format === 'string' && STICKER_FORMATS.has(item.sticker_format)
        ? { stickerFormat: item.sticker_format as StickerFormat }
        : {}),
      ...(type === 'sticker' && typeof item.emoji === 'string' && item.emoji.length > 0
        ? { emoji: item.emoji.slice(0, 16) }
        : {}),
    });
  }
  return result;
}

const KB = 1024;
const MB = KB * 1024;

/** Человеческий размер: Б до 1 КБ, иначе КБ/МБ с одним знаком после запятой. */
export function formatSize(bytes: number): string {
  if (bytes < KB) return `${Math.round(bytes)} Б`;
  if (bytes < MB) return `${Math.round((bytes / KB) * 10) / 10} КБ`;
  return `${Math.round((bytes / MB) * 10) / 10} МБ`;
}

export function typeLabel(type: AttachmentType): string {
  return TYPE_LABELS[type];
}

/** «<тип по-русски> <имя> <размер>» — отсутствующие части (имя/размер) просто опускаются. */
export function formatAttachmentLabel(att: ParsedAttachment): string {
  const parts = [TYPE_LABELS[att.type]];
  if (att.emoji !== undefined) parts.push(att.emoji);
  if (att.name !== null && att.type !== 'sticker') parts.push(att.name);
  if (att.size !== null) parts.push(formatSize(att.size));
  return parts.join(' ');
}
