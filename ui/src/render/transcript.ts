// Ф14-6b (docs/architecture.md, 14-6b «UI» и «Мелкое»): что показать под плеером голосового —
// чистая функция без Vue. Вход: разобранное вложение (вид, флаги `round`/`clip`) и `media` ядра со строкой
// `transcript`. Текст расшифровки — чужие слова: компонент выводит его только простым текстом
// (интерполяцией), не markdown и не как разметку.
import type { MediaItem } from '../api/types';
import type { ParsedAttachment } from './attachments';

/** Длиннее — свёрнуто, «показать целиком». */
export const TRANSCRIPT_FOLD_CHARS = 300;
/** Сомнительный результат движка без речи (`problem` ядра) — «речи не слышно». */
export const SILENCE_PROBLEM = 'пусто';

export type TranscriptState =
  | 'none' // строки нет
  | 'working' // queued/working — «расшифровываю…»
  | 'done' // текст
  | 'silence' // «речи не слышно»
  | 'failed'; // «не удалось расшифровать»

export interface TranscriptView {
  state: TranscriptState;
  /** Текст готовой расшифровки (при `failed`/`working` повтора — прежний удачный, если был). */
  text: string;
  /** Пометка языка — только `en` (русский — по умолчанию). */
  langMark: string | null;
  /** `problem` (кроме «пусто») — серой строкой «расшифровка может быть неточной». */
  doubtful: boolean;
  /** Кнопка: «Расшифровать» (строки нет), «Расшифровать заново» (`done`/`failed`), `null` — нет кнопки. */
  button: 'Расшифровать' | 'Расшифровать заново' | null;
}

/** Решение 6: по кнопке — голосовое, аудио, кружок, клип. Обычное видео — никогда. */
export function transcribable(att: ParsedAttachment, media: MediaItem | null): boolean {
  if (media === null || media.status !== 'ready') return false;
  if (att.type === 'voice' || att.type === 'audio') return true;
  if (att.round === true || att.clip === true) return true;
  return media.kind === 'voice' || media.kind === 'audio';
}

export function transcriptView(att: ParsedAttachment, media: MediaItem | null): TranscriptView {
  const t = media?.transcript ?? null;
  const can = transcribable(att, media);
  if (t === null) {
    return { state: 'none', text: '', langMark: null, doubtful: false, button: can ? 'Расшифровать' : null };
  }
  const text = typeof t.text === 'string' ? t.text : '';
  const langMark = t.lang === 'en' ? 'en' : null;
  if (t.status === 'queued' || t.status === 'working') {
    return { state: 'working', text, langMark, doubtful: false, button: null };
  }
  const again = can ? 'Расшифровать заново' : null;
  if (t.status === 'failed') {
    return { state: 'failed', text, langMark, doubtful: false, button: again };
  }
  if (t.problem === SILENCE_PROBLEM || text.trim() === '') {
    return { state: 'silence', text: '', langMark: null, doubtful: false, button: again };
  }
  return { state: 'done', text, langMark, doubtful: t.problem !== null && t.problem !== '', button: again };
}

/** Свёрнутый вид длинного текста: первые `TRANSCRIPT_FOLD_CHARS` символов и «…». */
export function foldTranscript(text: string): { short: string; folded: boolean } {
  if (text.length <= TRANSCRIPT_FOLD_CHARS) return { short: text, folded: false };
  return { short: `${text.slice(0, TRANSCRIPT_FOLD_CHARS).trimEnd()}…`, folded: true };
}

/** Ошибка кнопки — словами рядом с ней (ответ ядра: `detail`). */
export function transcribeErrorText(status: number | null, detail: string | null): string {
  if (status === 409 && detail === 'transcribe_busy') return 'уже расшифровываю';
  if (status === 409 && detail === 'media_not_ready') return 'сначала загрузи файл';
  if (status === 422) return 'этот файл не расшифровать';
  return 'не получилось, попробуй ещё раз';
}
