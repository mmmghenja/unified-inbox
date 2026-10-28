// Ф21-1b (`docs/architecture.md`): чистые функции текстов карточки человека и панели «Люди» —
// длительность, обращение, доли в %, строки цифр. Всё, что пришло от сервера (имена, ники, почты), сюда попадает
// только как данные для подстановки; выводится шаблонами текстом.
import type { PersonBrief, PersonProject, PersonSource, PersonStats, PersonStyle } from '../api/types';

const SOURCE_LABELS: Record<string, string> = { telegram: 'Telegram', slack: 'Slack', mattermost: 'Mattermost', whatsapp: 'WhatsApp', mail: 'Почта', feed: 'Лента' };

export const NO_NAME = '(без имени)';
export const NO_STATS_TEXT = 'Цифры появятся в течение часа.';

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

/** Значок источника в строке: короткая метка. */
export function sourceBadge(source: string): string {
  if (source === 'telegram') return 'TG';
  if (source === 'slack') return 'SL';
  if (source === 'mattermost') return 'MM';
  if (source === 'whatsapp') return 'WA';
  if (source === 'mail') return '@';
  if (source === 'feed') return 'ЛН';
  return source.slice(0, 2).toUpperCase();
}

/** Ник или почта источника для строки; обоих нет — пусто. */
export function sourceHandle(item: Pick<PersonSource, 'handle' | 'email'>): string {
  return item.handle ?? item.email ?? '';
}

function decimal(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return String(rounded).replace('.', ',');
}

/** `< 1 мин`, `12 мин`, `2 ч`, `1,5 ч`, `1,5 дн`. */
export function durationText(ms: number): string {
  if (!(ms > 0) || ms < 60_000) return '< 1 мин';
  const minutes = ms / 60_000;
  if (minutes < 60) return `${Math.round(minutes)} мин`;
  const hours = minutes / 60;
  if (hours < 24) return `${decimal(hours)} ч`;
  return `${decimal(hours / 24)} дн`;
}

/** Выбор формы слова: pluralize(5, ['раз', 'раза', 'раз']). */
export function plural(n: number, forms: [string, string, string]): string {
  const mod100 = n % 100;
  const mod10 = n % 10;
  if (mod100 >= 11 && mod100 <= 14) return forms[2];
  if (mod10 === 1) return forms[0];
  if (mod10 >= 2 && mod10 <= 4) return forms[1];
  return forms[2];
}

export function times(n: number): string {
  return `${n} ${plural(n, ['раз', 'раза', 'раз'])}`;
}

/** Доля 0..1 -> `20 %`. */
export function percent(share: number): string {
  return `${Math.round(share * 100)} %`;
}

export function addressText(address: PersonStyle['address']): string | null {
  if (address === 'ты') return 'на «ты»';
  if (address === 'вы') return 'на «вы»';
  if (address === 'смешанно') return 'то на «ты», то на «вы»';
  return null;
}

export function lengthText(medianLen: number): string {
  const word = medianLen <= 60 ? 'коротко' : medianLen <= 200 ? 'средне' : 'длинно';
  return `${word} (~${medianLen} знаков)`;
}

export function langText(lang: PersonStyle['lang']): string | null {
  if (lang === 'ru') return 'по-русски';
  if (lang === 'en') return 'по-английски';
  if (lang === 'mixed') return 'вперемешку по-русски и по-английски';
  return null;
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** `ДД.ММ` по местному времени. */
export function dayText(ts: number): string {
  const d = new Date(ts);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
}

/** `ЧЧ:ММ` по местному времени. */
export function clockText(ts: number): string {
  const d = new Date(ts);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** «Посчитано в 14:05» или «Цифры появятся в течение часа.». */
export function computedLine(statsTs: number | null): string {
  return statsTs === null ? NO_STATS_TEXT : `Посчитано в ${clockText(statsTs)}`;
}

function replyLine(prefix: string, part: PersonStats['reply_me']): string | null {
  const parts: string[] = [];
  if (part.n > 0) parts.push(`обычно за ${durationText(part.median_ms)} (${times(part.n)})`);
  if (part.unanswered > 0) parts.push(`без ответа ${part.unanswered}`);
  return parts.length === 0 ? null : `${prefix}: ${parts.join(', ')}`;
}

/** «Ты отвечаешь: обычно за 12 мин (18 раз), без ответа 2»; нет данных — `null` (строка пропадает). */
export function replyMeLine(stats: PersonStats): string | null {
  return replyLine('Ты отвечаешь', stats.reply_me);
}

export function replyThemLine(stats: PersonStats): string | null {
  return replyLine('Отвечает тебе', stats.reply_them);
}

/** «За 30 дней: 7 от него, 5 от тебя · последний раз 01.10». */
export function periodLine(stats: PersonStats): string | null {
  const last = Math.max(stats.in.last_ts ?? 0, stats.out.last_ts ?? 0);
  if (stats.in.d30 === 0 && stats.out.d30 === 0 && last === 0) return null;
  const head = `За 30 дней: ${stats.in.d30} от него, ${stats.out.d30} от тебя`;
  return last > 0 ? `${head} · последний раз ${dayText(last)}` : head;
}

/** «Как ты пишешь (чаты): на «ты», коротко (~40 знаков), смайлы в 20 %, приветствие в 10 %». */
export function styleLine(kind: 'chat' | 'mail', style: PersonStyle | null): string | null {
  if (style === null || style.n === 0) return null;
  const parts: string[] = [];
  const address = addressText(style.address);
  if (address !== null) parts.push(address);
  parts.push(lengthText(style.median_len));
  parts.push(`смайлы в ${percent(style.emoji_share)}`);
  parts.push(`приветствие в ${percent(style.greet_share)}`);
  const lang = langText(style.lang);
  if (lang !== null) parts.push(lang);
  return `Как ты пишешь (${kind === 'chat' ? 'чаты' : 'письма'}): ${parts.join(', ')}`;
}

/** «Реакции: ты ему 👍 5 · ❤️ 2; он тебе 🔥 3». */
export function reactionsLine(stats: PersonStats): string | null {
  const side = (items: PersonStats['react']['me_to_them']) => items.map((r) => `${r.key} ${r.n}`).join(' · ');
  const out: string[] = [];
  if (stats.react.me_to_them.length > 0) out.push(`ты ему ${side(stats.react.me_to_them)}`);
  if (stats.react.them_to_me.length > 0) out.push(`он тебе ${side(stats.react.them_to_me)}`);
  return out.length === 0 ? null : `Реакции: ${out.join('; ')}`;
}

/** Все строки цифр карточки в порядке показа; пустые пропадают. */
export function statsLines(stats: PersonStats): string[] {
  return [
    replyMeLine(stats),
    replyThemLine(stats),
    periodLine(stats),
    styleLine('chat', stats.style.chat),
    styleLine('mail', stats.style.mail),
    reactionsLine(stats),
  ].filter((line): line is string => line !== null);
}

/** Названия проектов человека (без скрытых `null`). */
export function projectTitles(projects: PersonProject[]): string[] {
  return projects.map((p) => p.title).filter((t): t is string => t !== null && t !== '');
}

/** «Отвечаешь ~12 мин» для строки панели; нет данных — `null`. */
export function replyMeBrief(brief: Pick<PersonBrief, 'reply_me_median_ms'>): string | null {
  return brief.reply_me_median_ms === null ? null : `отвечаешь ~${durationText(brief.reply_me_median_ms)}`;
}

/** «Последний контакт 01.10». */
export function lastContactText(brief: Pick<PersonBrief, 'last_ts'>): string | null {
  return brief.last_ts === null ? null : `последний контакт ${dayText(brief.last_ts)}`;
}

/** Подпись в предложении склейки: «Иван (Telegram)» — имя и источники. */
export function briefWithSources(brief: Pick<PersonBrief, 'name' | 'sources'>): string {
  const names = [...new Set(brief.sources.map((s) => sourceLabel(s.source)))];
  return names.length === 0 ? brief.name : `${brief.name} (${names.join(', ')})`;
}

/** «A (Telegram) и B (Slack)». */
export function suggestionLine(a: PersonBrief, b: PersonBrief): string {
  return `${briefWithSources(a)} и ${briefWithSources(b)}`;
}

/** Подтверждение объединения: «Объединить X и Y?». */
export function mergeConfirm(into: string, from: string): string {
  return `Объединить ${into} и ${from}?`;
}

/** Текст ошибки ответа: 422 — причина ядра, иначе общий. */
export function personErrorText(status: number | null, message: string): string {
  if (status === 422) return message || 'Нельзя: эти люди помечены как разные.';
  if (status === 404) return 'Человек не найден — возможно, уже объединён. Обнови список.';
  return 'Не удалось — попробуй ещё раз.';
}

// --- Ф24-1c: факты о человеке --------------------------------------------------------------------------------

export const FACTS_EMPTY = 'Хаб ещё ничего не записал: факты появятся после переписки';
export const FACTS_CAPTION = 'Хаб собирает сам по вашей переписке; правка закрепляет факт';
export const FACT_REMOVE_CONFIRM = 'Убрать факт? Хаб не будет добавлять его снова';

/** Дата факта — подсказкой (`title`). */
export function factDate(ts: number): string {
  return new Date(ts).toLocaleString('ru-RU', { dateStyle: 'medium', timeStyle: 'short' });
}
