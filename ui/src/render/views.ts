// Ф23-3: просмотры поста канала Telegram — поле `views` сообщения (`{views, forwards, ts}` или `null`). Под постом
// рядом с реакциями — «👁 1 234» (разделитель тысяч — неразрывный пробел), подсказка «просмотры на ЧЧ:ММ ·
// пересылок N». Вход недоверенный: не число — ничего не показываем.
import type { PostViews } from '../api/types';

const NBSP = ' ';

function isCount(v: unknown): v is number {
  return typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
}

/** Разобрать поле `views` сообщения; нет его или мусор — `null`. */
export function parseViews(raw: unknown): PostViews | null {
  if (raw === null || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  if (!isCount(r.views)) return null;
  return {
    views: r.views,
    forwards: isCount(r.forwards) ? r.forwards : null,
    ts: isCount(r.ts) ? r.ts : null,
  };
}

/** «1 234 567» — группы по три цифры через неразрывный пробел. */
export function formatCount(n: number): string {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
}

/** «👁 1 234». */
export function viewsLabel(v: PostViews): string {
  return `👁${NBSP}${formatCount(v.views)}`;
}

/** «просмотры на 15:59 · пересылок 5»: время снятия — локальное; нет времени или пересылок — без них. */
export function viewsTitle(v: PostViews): string {
  let text = 'просмотры';
  if (v.ts !== null) {
    text += ` на ${new Date(v.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
  }
  if (v.forwards !== null) text += ` · пересылок ${formatCount(v.forwards)}`;
  return text;
}
