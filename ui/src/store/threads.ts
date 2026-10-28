// Общая логика тредов Slack на стороне UI (спека `docs/architecture.md`).
// Чистый модуль — БЕЗ импорта Vue (тот же принцип, что `applySync.ts`/`feedRows.ts`/`visibleMessage.ts`):
// тестируется на голых объектах, `store.ts` мутирует уже `reactive(...)`-обёрнутое состояние через эти же
// функции, реактивность срабатывает у вызывающей стороны бесплатно.
import type { Message, ThreadMessagePage } from '../api/types';

// --- порядок показа (перенесено из applySync.ts, чтобы им могли пользоваться и лента, и тред без
// циклического импорта между двумя модулями — applySync.ts импортирует эту функцию отсюда и реэкспортирует
// её под тем же именем ради `visibleMessage.ts`, который её импортирует). Правило то же: `(ts, ext_sort, id)`.
//
// Находка U2 opus-критика (2b, закрыта в кусок 2e): `ext_sort` ЗДЕСЬ обязан считаться ТЕМ ЖЕ правилом, что
// `src/hub/core/extsort.py::ext_sort` на сервере — раньше воспроизводился только числовой Telegram-случай, и
// два ответа Slack с одинаковым `ts` (миллисекунды) сортировались по `id` вместо реальных микросекунд.
// Правило: ASCII-цифры длиной ≤ 18 (Telegram) -> число; `ts` Slack (`^\d{1,12}\.\d{6}$`, СТРОГО 6 цифр дроби)
// -> `sec * 1_000_000 + frac`; иначе (в т.ч. `null` у заготовки outbox) -> 0.
// Точность `Number`: реальный `ts` Slack даёт значения порядка `1.7e9 * 1e6 ≈ 1.7e15` — это меньше
// `Number.MAX_SAFE_INTEGER` (2^53-1 ≈ 9.007e15), переполнение возможно только при `ts` > ~9e9 секунд
// (год ~2255) — не наша забота.
const SLACK_TS_RE = /^(\d{1,12})\.(\d{6})$/;

function extSortOf(extId: string | null): number {
  if (extId === null) return 0;
  if (/^[0-9]{1,18}$/.test(extId)) return Number(extId);
  const m = SLACK_TS_RE.exec(extId);
  if (m) return Number(m[1]) * 1_000_000 + Number(m[2]);
  return 0;
}

export function compareMessages(a: Message, b: Message): number {
  if (a.ts !== b.ts) return a.ts - b.ts;
  const sa = extSortOf(a.ext_id);
  const sb = extSortOf(b.ext_id);
  if (sa !== sb) return sa - sb;
  return a.id - b.id;
}

// --- единый предикат ответа треда — версия на значениях JS того же правила, что `core/threads.py`:
// `is_reply := thread_ext_id IS NOT NULL AND thread_ext_id IS NOT ext_id` (спека 2b, раздел «Модель
// тредов»; `!==` в JS уже даёт нужную NULL-безопасность, как `!=` в Python). ------------------------------

export function isReply(message: Pick<Message, 'thread_ext_id' | 'ext_id'>): boolean {
  return message.thread_ext_id !== null && message.thread_ext_id !== message.ext_id;
}

/** Спека 2e, раздел «Семантика «Ответить» по источнику», ветка Slack: тред, который открывает клик
 * «Ответить» на сообщении Slack-канала — `thread_ext_id` сообщения, если оно САМО ответ (`isReply`), иначе
 * его собственный `ext_id` (сообщение top-level, включая broadcast, — открываем/заводим его тред). */
export function replyTargetThreadExtId(message: Pick<Message, 'thread_ext_id' | 'ext_id'>): string | null {
  if (isReply(message)) return message.thread_ext_id;
  return message.ext_id;
}

/** Ф40-4 A2: у каких источников «Ответить» на сообщении канала открывает тред (Slack, Mattermost), а не плашку
 * ответа в поле ввода (Telegram, почта). */
export function opensThreadOnReply(source: string | null | undefined): boolean {
  return source === 'slack' || source === 'mattermost';
}

export function threadKey(channelId: number, threadExtId: string): string {
  return `${channelId}:${threadExtId}`;
}

export interface ThreadState {
  parent: Message | null;
  replies: Message[];
  nextBefore: string | null;
  loading: boolean;
}

export type ThreadsMap = Record<string, ThreadState>;

export function createThreadsState(): ThreadsMap {
  return {};
}

export interface OpenThreadRef {
  channelId: number;
  threadExtId: string;
}

function upsertReply(list: Message[], message: Message): void {
  const idx = list.findIndex((m) => m.id === message.id);
  if (idx === -1) list.push(message);
  else list[idx] = message;
  list.sort(compareMessages);
}

/** Раздел «Стор» спеки 2e: ответ (`isReply`) -> upsert в `threads[key].replies`, ТОЛЬКО если этот тред уже
 * загружен (ключ есть в `threads`) — иначе no-op (тред, который никто не открывал, не заводится сам от
 * пролетающего мимо `/sync`). */
export function upsertThreadReply(threads: ThreadsMap, message: Message): void {
  if (message.thread_ext_id === null) return;
  const key = threadKey(message.channel_id, message.thread_ext_id);
  const thread = threads[key];
  if (thread === undefined) return;
  upsertReply(thread.replies, message);
}

/** Родитель обновился (сообщение НЕ ответ, но у него может быть свой тред) -> обновить `threads[key].parent`,
 * если этот тред уже загружен. Ключ треда родителя — его СОБСТВЕННЫЙ `ext_id` (сводка треда живёт под ним). */
export function updateThreadParentIfLoaded(threads: ThreadsMap, message: Message): void {
  if (message.ext_id === null) return;
  const key = threadKey(message.channel_id, message.ext_id);
  const thread = threads[key];
  if (thread !== undefined) thread.parent = message;
}

/** Слияние страницы `GET /channels/{id}/threads/{ext}/messages` в состояние треда — та же дедуп-логика по
 * `id`, что у ленты канала (`mergeMessagePage` в applySync.ts): повторная загрузка страницы не даёт дублей.
 * Заводит запись треда, если её ещё не было (вызывающая сторона уже могла завести её как «loading» заранее —
 * тогда просто дозаполняет). `parent: null` страницы НЕ затирает уже известного родителя (см. раздел
 * «Панель треда»: родитель может подъехать отдельным сообщением ленты, а не только этим ответом API). */
export function mergeThreadMessagePage(
  threads: ThreadsMap,
  channelId: number,
  threadExtId: string,
  page: ThreadMessagePage,
): void {
  const key = threadKey(channelId, threadExtId);
  let thread = threads[key];
  if (thread === undefined) {
    thread = { parent: null, replies: [], nextBefore: null, loading: false };
    threads[key] = thread;
  }
  for (const message of page.messages) {
    upsertReply(thread.replies, message);
  }
  thread.nextBefore = page.next_before;
  if (page.parent !== null) thread.parent = page.parent;
}
