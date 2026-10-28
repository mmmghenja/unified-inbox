// Ф8-5 B (docs/architecture.md): что показать на месте каждого вложения — чистая функция без Vue,
// тестируется на голых объектах. Вход: разобранные вложения сообщения (`attachments.ts`, с исходным `idx`) и
// `media` сообщения от ядра (статус загрузки, `detected`). Показ выбирается по `detected` (сигнатура байтов на
// сервере), не по `kind` и не по `mime` источника (П15): видео-стикер webm и GIF Telegram (mp4) — видео без
// звука по кругу, PSD/TIFF/HEIC/SVG (`detected=other`) — строка «Скачать» (П7).
import type { MediaItem } from '../api/types';
import type { ParsedAttachment } from './attachments';

export type MediaDisplay =
  | 'image' // готовая картинка — превью в ленте, клик — просмотр
  | 'video' // <video controls>
  | 'sticker' // стикер Telegram с известным форматом: <img> / <video> / Lottie, ≈160 px (StickerView)
  | 'loop' // GIF/видео-стикер: <video muted autoplay loop playsinline>
  | 'audio' // <audio controls>
  | 'file' // «<имя> · <размер> · Скачать»
  | 'loading' // pending/fetching — заглушка «загружается…»
  | 'fetch' // deferred или старое вложение без строки — «Загрузить (<размер>)»
  | 'failed' // «не удалось загрузить · Повторить»
  | 'gone' // «файла больше нет в Telegram/Slack»
  | 'too_big' // «слишком большой (<размер>)»
  | 'ttl' // одноразовое Telegram — «одноразовое — смотри в Telegram»
  | 'external' // внешний файл Slack — «внешний файл — открыть в Slack»
  | 'none'; // тип без файла (геопозиция, контакт, опрос) или источник без медиа — просто подпись

export interface AttachmentView {
  messageId: number;
  att: ParsedAttachment;
  media: MediaItem | null;
  display: MediaDisplay;
  /** Размер для надписей: из `media` (точнее — фактический после скачивания), иначе из вложения. */
  size: number | null;
}

const IMAGE_DETECTED: ReadonlySet<string> = new Set(['jpeg', 'png', 'webp', 'gif']);
const VIDEO_DETECTED: ReadonlySet<string> = new Set(['mp4', 'webm']);
const AUDIO_DETECTED: ReadonlySet<string> = new Set(['ogg', 'mp3', 'm4a']);
const NO_FILE_TYPES: ReadonlySet<string> = new Set(['location', 'contact', 'poll', 'other']);
const LOOP_TYPES: ReadonlySet<string> = new Set(['gif', 'sticker']);

/** П14: «Загрузить» — только у источников с медиа. Ф11-3a A4: и у почты (вложения писем — по клику). */
export const MEDIA_SOURCES: ReadonlySet<string> = new Set(['telegram', 'slack', 'mattermost', 'mail', 'whatsapp']);

/** Ф14-4 D3: голосовое/аудио в webm (запись браузера) — звук, не видео. */
const AUDIO_TYPES: ReadonlySet<string> = new Set(['voice', 'audio']);

export type StickerKind = 'img' | 'video' | 'lottie';

/** Чем рисовать стикер: по сигнатуре файла, а `.tgs` (gzip, `detected=other`) — только при формате `animated`. */
export function stickerKind(att: ParsedAttachment, detected: string): StickerKind | null {
  if (detected === 'webp' || detected === 'png' || detected === 'jpeg' || detected === 'gif') return 'img';
  if (detected === 'webm' || detected === 'mp4') return 'video';
  if (detected === 'other' && att.stickerFormat === 'animated') return 'lottie';
  return null;
}

function readyDisplay(att: ParsedAttachment, media: MediaItem): MediaDisplay {
  const detected = media.detected ?? 'other';
  // Новые стикеры (с `sticker_format`) — одним видом «sticker»; старые без формата — как раньше.
  if (att.type === 'sticker' && att.stickerFormat !== undefined && stickerKind(att, detected) !== null) {
    return 'sticker';
  }
  if (IMAGE_DETECTED.has(detected)) return 'image';
  if (detected === 'webm' && AUDIO_TYPES.has(att.type)) return 'audio';
  if (VIDEO_DETECTED.has(detected)) return LOOP_TYPES.has(att.type) ? 'loop' : 'video';
  if (AUDIO_DETECTED.has(detected)) return 'audio';
  return 'file';
}

export function displayFor(att: ParsedAttachment, media: MediaItem | null, source: string | null): MediaDisplay {
  if (NO_FILE_TYPES.has(att.type)) return 'none';
  if (att.ttl) return 'ttl';
  if (att.external) return 'external';
  if (media === null) {
    if (att.gone) return 'gone';
    return source !== null && MEDIA_SOURCES.has(source) ? 'fetch' : 'none';
  }
  switch (media.status) {
    case 'ready':
      return readyDisplay(att, media);
    case 'pending':
    case 'fetching':
      return 'loading';
    case 'deferred':
      return 'fetch';
    case 'failed':
      return 'failed';
    case 'gone':
      return 'gone';
    case 'too_big':
      return 'too_big';
    default:
      return 'none';
  }
}

export function buildAttachmentViews(
  messageId: number,
  attachments: ParsedAttachment[],
  media: MediaItem[] | undefined,
  source: string | null,
): AttachmentView[] {
  const byIdx = new Map<number, MediaItem>();
  for (const m of media ?? []) byIdx.set(m.idx, m);
  return attachments.map((att) => {
    const item = byIdx.get(att.idx) ?? null;
    return {
      messageId,
      att,
      media: item,
      display: displayFor(att, item, source),
      size: item?.size ?? att.size,
    };
  });
}

export const PREVIEW_BOX_PX = 320;

/** Размер превью «вписать в 320×320» по пропорциям источника (лента не прыгает, B1/B4). Неизвестны — `null`
 * (тогда ограничивает CSS). */
export function previewSize(
  width: number | null | undefined,
  height: number | null | undefined,
  box = PREVIEW_BOX_PX,
): { width: number; height: number } | null {
  if (!width || !height || width <= 0 || height <= 0) return null;
  const scale = Math.min(1, box / width, box / height);
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) };
}

export function sourceLabel(source: string | null): string {
  if (source === 'telegram') return 'Telegram';
  if (source === 'slack') return 'Slack';
  if (source === 'mattermost') return 'Mattermost';
  if (source === 'whatsapp') return 'WhatsApp';
  if (source === 'mail') return 'почте';
  return 'источнике';
}
