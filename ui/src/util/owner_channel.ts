// Ф56-3: очередь владельца — чистые функции окна «Агент». Все тексты (`brief`, `full`, пути, роли) — чужой ввод,
// маскированный ядром: сюда они попадают только строками для текстовой интерполяции.
import type { OwnerErrand, OwnerErrandReturned, OwnerItem } from '../api/types';

/** Срок ближе этого — карточка «срочная» (первой, красным). */
export const OWNER_URGENT_MS = 10 * 60 * 1000;
/** Полный текст короче этого показывается внутри карточки, длиннее — окном поверх. */
export const OWNER_INLINE_MAX = 600;

export const KIND_RU: Record<string, string> = {
  permission: 'разрешение',
  question: 'вопрос',
  human: 'нужен человек',
  draft: 'черновик',
  press: 'кнопка бота',
  join: 'вступление',
  leave: 'выход',
  group_op: 'группа WhatsApp',
  rule: 'правило',
  agent_request: 'заявка агента',
  reminder: 'напоминание',
  heavy: 'тяжёлая',
  paused: 'на паузе',
  interrupted: 'прерванная реплика',
  turn: 'ответ сессии',
  agent_msg: 'сообщение агента',
  stall: 'стоит без дела',
  broken: 'ход оборван',
  dead: 'не запускается',
  stuck: 'завис',
  errand: 'поручение',
};

export function kindLabel(kind: string): string {
  return KIND_RU[kind] ?? kind;
}

export function isUrgentOwner(item: OwnerItem, nowMs: number): boolean {
  return item.until_ts !== null && item.until_ts - nowMs < OWNER_URGENT_MS;
}

function rank(item: OwnerItem, nowMs: number): number {
  if (isUrgentOwner(item, nowMs)) return 0;
  if (item.kind === 'human') return 1;
  if (item.kind === 'permission' || item.kind === 'question') return 2;
  return 3;
}

/** Срочное → «нужен человек» → разрешения и вопросы → остальное; внутри — старые выше. */
export function sortOwner(items: OwnerItem[], nowMs: number): OwnerItem[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const ra = rank(a.item, nowMs);
      const rb = rank(b.item, nowMs);
      if (ra !== rb) return ra - rb;
      if (ra === 0 && a.item.until_ts !== b.item.until_ts) return (a.item.until_ts ?? 0) - (b.item.until_ts ?? 0);
      return a.item.created_ts - b.item.created_ts || a.index - b.index;
    })
    .map((x) => x.item);
}

export type OwnerFilter = 'all' | 'channel' | 'mine';

/** «в канале» — карточки у агента; «только мне» — те, что агент не берёт. */
export function filterOwner(items: OwnerItem[], filter: OwnerFilter): OwnerItem[] {
  if (filter === 'channel') return items.filter((i) => i.in_channel);
  if (filter === 'mine') return items.filter((i) => !i.in_channel);
  return items;
}

/** «проект › голова › автор · о <роль>»: пустые звенья пропускаются. */
export function pathLine(item: OwnerItem): string {
  const p = item.path;
  const parts = [p.project, p.head, p.author].filter((x): x is string => typeof x === 'string' && x !== '');
  let line = parts.join(' › ');
  if (typeof p.about === 'string' && p.about !== '') line += `${line === '' ? '' : ' · '}о ${p.about}`;
  return line;
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

/** «12 мин» / «2 ч» / «3 д» с создания карточки. */
export function ageLabel(createdTs: number, nowMs: number): string {
  const min = Math.max(0, Math.floor((nowMs - createdTs) / 60000));
  if (min < 1) return 'сейчас';
  if (min < 60) return `${min} мин`;
  if (min < 24 * 60) return `${Math.floor(min / 60)} ч`;
  return `${Math.floor(min / 1440)} д`;
}

/** «до ЧЧ:ММ» по `until_ts`; без срока — `null`. */
export function untilLabel(item: OwnerItem): string | null {
  if (item.until_ts === null) return null;
  const d = new Date(item.until_ts);
  return `до ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** «передал <роль>: <текст>» — `full.passed` у разрешения и вопроса после «спросить владельца». */
export function passedLine(item: OwnerItem): string | null {
  const passed = item.full.passed;
  if (passed === null || typeof passed !== 'object') return null;
  const rec = passed as Record<string, unknown>;
  const role = typeof rec.role === 'string' ? rec.role : '';
  const text = typeof rec.text === 'string' ? rec.text : '';
  if (role === '' && text === '') return null;
  return `передал ${role}${text === '' ? '' : `: ${text}`}`;
}

/** Ф58-3: «решает агент: <действие> · <последний прогресс> · N мин» — время от создания поручения. Карточка
 *  очереди и заявка «нужен человек» (поле `errand`). */
export function errandLine(errand: OwnerErrand | null | undefined, nowMs: number): string | null {
  if (errand === null || errand === undefined) return null;
  const parts = [errand.label ?? errand.text ?? 'поручение'];
  if (errand.progress) parts.push(errand.progress);
  parts.push(ageLabel(errand.created_ts, nowMs));
  // Ф96-1: руки у владельца — агент ждёт, решает не он.
  if (errand.hands === 'owner_channel') return `ждёт тебя (руки у тебя): ${parts.join(' · ')}`;
  return `решает агент: ${parts.join(' · ')}`;
}

/** Ф58-3: отпечаток поручений по заявкам «нужен человек» в очереди: изменился — заявки пора перечитать. */
export function humanErrandsMark(items: OwnerItem[]): string {
  return items
    .filter((i) => i.kind === 'human')
    .map((i) => `${i.ref}:${i.errand?.id ?? ''}:${i.errand?.progress ?? ''}:${i.errand_returned?.ts ?? ''}`)
    .join('|');
}

/** Ф58-3/Ф65-1: заголовок возврата. `agent` — «Агент вернул тебе: <причина>» (правки 15.10); `system` — «Агент
 *  завис: <что>» (ядро пишет причину «завис: …» — приставка не дублируется); `ui` — причина владелец уже видит
 *  («остановил владелец»). */
export function returnedHead(returned: OwnerErrandReturned): string {
  if (returned.by === 'system') return `Агент завис: ${returned.reason.replace(/^завис:\s*/, '')}`;
  if (returned.by === 'ui') return returned.reason;
  return `Агент вернул тебе: ${returned.reason}`;
}

/** Ф58-3: «<заголовок возврата> · ЧЧ:ММ». */
export function returnedLine(returned: OwnerErrandReturned | null | undefined): string | null {
  if (returned === null || returned === undefined) return null;
  const d = new Date(returned.ts);
  return `${returnedHead(returned)} · ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Ф65-1: подробности возврата (`detail`) свёрнуты до стольких строк, дальше — «развернуть». */
export const RETURNED_DETAIL_LINES = 3;

/** Ф66-1: группы таблицы «Кто отвечает на карточки» — ключи `card_route` ядра, подписи интерфейса; `auto` — группа
 *  допускает «Сразу да» (`card_route.AUTO_GROUPS` ядра: разрешения, черновики, кнопки ботов, вступления и группы, заявки,
 *  состояния сессий, напоминания, ответ сессии). Остальным ядро отвечает 422. `hint` — что значит «Сразу да» у группы. */
export const CARD_ROUTE_GROUPS: readonly {
  key: string;
  label: string;
  auto: boolean;
  hint?: string;
  /** Ф93-1: `false` — строку не трогает массовое «всем: Мне / Агенту» и не учитывает его подсветка. */
  bulk?: boolean;
}[] = [
  { key: 'permission', label: 'Разрешения', auto: true },
  { key: 'question', label: 'Вопросы сессий', auto: false },
  {
    key: 'turn',
    label: 'Ответ сессии',
    auto: true,
    hint: '«Сразу да»: ответ без «ВЛАДЕЛЬЦУ:» — без карточки, со «ВЛАДЕЛЬЦУ:» — агенту',
  },
  { key: 'human', label: 'Нужен человек', auto: false },
  { key: 'draft', label: 'Черновики', auto: true },
  { key: 'press', label: 'Кнопки ботов', auto: true },
  { key: 'join', label: 'Вступления и группы', auto: true },
  { key: 'requests', label: 'Заявки', auto: true },
  { key: 'states', label: 'Состояния сессий', auto: true },
  { key: 'reminder', label: 'Напоминания', auto: true },
  { key: 'agent_msg', label: 'Сообщения агентов', auto: false },
  {
    key: 'money',
    label: 'Деньги',
    auto: false,
    bulk: false,
    hint: 'Оплата, покупки, подписки — ответит только тот, кого выберешь. По умолчанию — ты',
  },
];

/** Ф93-1: строки таблицы, которые меняет массовое «всем: Мне / Агенту» (без «Деньги»). */
export const CARD_ROUTE_BULK_GROUPS = CARD_ROUTE_GROUPS.filter((g) => g.bulk !== false);

export const CARD_ROUTE_NOTE = 'Ручные сессии — всегда тебе.';

/** Кто решил закрытую карточку: «сразу да» (ядро, `payload.by = 'auto'`), агент, ты; иначе — не выдумываем (`null`). */
export function decidedByLabel(row: { closed_by: string | null; payload?: { by?: string } | null }): string | null {
  if (row.payload?.by === 'auto') return 'сразу да';
  if (row.closed_by === 'agent') return 'агент';
  if (row.closed_by === 'ui') return 'ты';
  return null;
}

export function stringify(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

// --- `full` по видам: подписанные по-русски поля -----------------------------------------------------------
// Приёмка Ф56, круг 3: сырых ключей (`id`, `kind`, `expires_ts`…) и чисел времени владельцу не показываем. У каждого
// вида — свой список полей с русской подписью и видом значения; служебное (номера, статусы, отметки времени
// оболочки) в список не входит вовсе.

/** Вид значения поля: текст; время «ЧЧ:ММ»; «N мин назад»; минуты; число; список (строки через запятую или
 *  число элементов); шаг (строка или `{tool}`). */
type FieldFmt = 'text' | 'time' | 'ago' | 'min' | 'num' | 'list' | 'step';

export interface FullField {
  key: string;
  label: string;
  value: string;
}

type FieldSpec = readonly [path: string, label: string, fmt?: FieldFmt];

const HUMAN_KIND_RU: Record<string, string> = {
  captcha: 'капча',
  login: 'вход',
  code: 'код',
  payment: 'оплата',
  consent: 'согласие',
  other: 'другое',
};

const REQUEST_FIELDS: readonly FieldSpec[] = [
  ['rule', 'правило'],
  ['file.path', 'файл'],
];

const FULL_FIELDS: Record<string, readonly FieldSpec[]> = {
  permission: REQUEST_FIELDS,
  question: [],
  human: [],
  draft: [
    ['channel_title', 'беседа'],
    ['new_to', 'кому'],
    ['author_role', 'автор'],
    ['reply_to.excerpt', 'ответ на'],
    ['thread.excerpt', 'в треде'],
    ['files', 'файлы', 'list'],
    ['send_at_ts', 'уйдёт в', 'time'],
  ],
  press: [
    ['channel_title', 'беседа'],
    ['text', 'кнопка'],
    ['quote', 'сообщение бота'],
    ['reason', 'зачем'],
  ],
  join: [
    ['title', 'куда'],
    ['target', 'адрес'],
    ['reason', 'зачем'],
    ['error', 'ошибка'],
  ],
  leave: [
    ['title', 'откуда'],
    ['reason', 'зачем'],
    ['error', 'ошибка'],
  ],
  group_op: [
    ['title', 'группа'],
    ['kind', 'что'],
    ['participants', 'участники', 'list'],
    ['reason', 'зачем'],
    ['error', 'ошибка'],
  ],
  rule: [
    ['description', 'правило'],
    ['value', 'условие'],
    ['account_title', 'аккаунт'],
    ['who', 'просит'],
    ['reason', 'зачем'],
    ['moved', 'переложит бесед', 'num'],
    ['examples', 'например', 'list'],
  ],
  agent_request: [
    ['who', 'просит'],
    ['reason', 'зачем'],
    ['role', 'роль'],
    ['payload.role', 'роль'],
    ['payload.task', 'задание'],
    ['payload.channel_title', 'беседа'],
    ['payload.engine', 'движок'],
    ['payload.model', 'модель'],
    ['payload.mode', 'режим'],
    ['payload.subdir', 'папка'],
    ['payload.role_file', 'файл роли'],
    ['subdir_warning', 'внимание'],
    ['role_file_warning', 'внимание'],
    ['note', 'заметка'],
  ],
  reminder: [
    ['channel.title', 'беседа'],
    ['note', 'заметка'],
    ['text', 'заметка'],
    ['due_ts', 'срок был в', 'time'],
  ],
  heavy: [
    ['title', 'сессия'],
    ['pending', 'сигналов ждёт', 'num'],
  ],
  paused: [
    ['title', 'сессия'],
    ['pending', 'сигналов ждёт', 'num'],
    ['queued_says', 'реплик в очереди', 'num'],
  ],
  interrupted: [
    ['text', 'реплика'],
    ['files', 'файлы', 'list'],
    ['interrupted_ts', 'прервана', 'ago'],
  ],
  turn: [],
  agent_msg: [['about_role', 'о роли']],
  stall: [
    ['silent_min', 'молчит', 'min'],
    ['subs', 'подписок', 'list'],
    ['reminders', 'напоминаний', 'list'],
    ['wake_at_ts', 'проснётся в', 'time'],
  ],
  broken: [
    ['note', 'причина'],
    ['step', 'шаг', 'step'],
    ['since_ts', 'ход начат', 'ago'],
    ['expired', 'истекло запросов', 'num'],
  ],
  dead: [
    ['failure', 'ошибка'],
    ['fails', 'попыток', 'num'],
    ['engine', 'движок'],
  ],
  stuck: [
    ['step', 'шаг', 'step'],
    ['since_ts', 'не возвращается', 'ago'],
    ['turn_since_ts', 'ход начат', 'ago'],
  ],
  errand: [
    ['label', 'поручение'],
    ['text', 'текст'],
    ['progress', 'ход работы'],
  ],
};

/** Ключи, которые владельцу не показываются никогда (запасной рендер неизвестного вида). */
const SERVICE_KEYS = new Set([
  'id', 'kind', 'seq', 'status', 'state', 'wait_reason', 'cut', 'turns', 'by_ui_say', 'origin', 'auto', 'own',
  'attempts', 'row', 'col', 'by', 'role_no', 'token_name', 'source', 'client_name', 'go_version', 'target_kind',
  'chat_kind', 'ext_id', 'ask', 'reply_to', 'passed', 'shot', 'tool',
]);

export function isServiceKey(key: string): boolean {
  return SERVICE_KEYS.has(key) || /(_ts|_id|_ids|_at)$/.test(key);
}

function dig(full: Record<string, unknown>, path: string): unknown {
  let value: unknown = full;
  for (const part of path.split('.')) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return value;
}

function clock(ms: number): string {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function listText(value: unknown): string {
  if (typeof value === 'number') return value > 0 ? String(value) : '';
  if (!Array.isArray(value) || value.length === 0) return '';
  const names = value.map((v) => {
    if (typeof v === 'string') return v;
    if (v !== null && typeof v === 'object' && typeof (v as Record<string, unknown>).name === 'string') {
      return (v as Record<string, unknown>).name as string;
    }
    // Ф62-6: участник группы без имени — маска номера.
    if (v !== null && typeof v === 'object' && typeof (v as Record<string, unknown>).mask === 'string') {
      return (v as Record<string, unknown>).mask as string;
    }
    return null;
  });
  return names.every((n): n is string => n !== null) ? names.join(', ') : String(value.length);
}

function fieldText(value: unknown, fmt: FieldFmt, nowMs: number): string {
  if (value === null || value === undefined || value === '') return '';
  switch (fmt) {
    case 'time':
      return typeof value === 'number' ? clock(value) : '';
    case 'ago': {
      if (typeof value !== 'number') return '';
      const age = ageLabel(value, nowMs);
      return age === 'сейчас' ? 'только что' : `${age} назад`;
    }
    case 'min':
      return typeof value === 'number' ? `${value} мин` : '';
    case 'num':
      return typeof value === 'number' && value > 0 ? String(value) : '';
    case 'list':
      return listText(value);
    case 'step': {
      if (typeof value === 'string') return value;
      const tool = typeof value === 'object' ? (value as Record<string, unknown>).tool : null;
      return typeof tool === 'string' ? tool : '';
    }
    default:
      if (typeof value === 'string') return value;
      if (typeof value === 'number') return String(value);
      if (typeof value === 'object' && typeof (value as Record<string, unknown>).title === 'string') {
        return (value as Record<string, unknown>).title as string;
      }
      return '';
  }
}

/** Подписанные поля `full` карточки. Известный вид — только поля из его списка; неизвестный — строковые
 *  значения неслужебных ключей (запасной путь). Поле, чей текст уже есть в `brief`, пропускается. */
export function fullFields(item: OwnerItem, nowMs: number, dedup = true): FullField[] {
  const spec = FULL_FIELDS[item.kind];
  const out: FullField[] = [];
  const seen = new Set<string>();
  const brief = item.brief ?? '';
  const push = (key: string, label: string, value: string): void => {
    const text = value.trim();
    if (text === '' || seen.has(`${label}\n${text}`)) return;
    if (dedup && text.length >= 4 && brief.includes(text)) return;
    seen.add(`${label}\n${text}`);
    out.push({ key, label, value: text });
  };
  if (spec === undefined) {
    for (const [key, value] of Object.entries(item.full)) {
      if (typeof value === 'string' && !isServiceKey(key)) push(key, key, value);
    }
    return out;
  }
  for (const [path, label, fmt] of spec) push(path, label, fieldText(dig(item.full, path), fmt ?? 'text', nowMs));
  return out;
}

/** Подпись вида заявки «нужен человек» («капча», «вход»…); неизвестный — пусто. */
export function humanKindLabel(full: Record<string, unknown>): string {
  return typeof full.kind === 'string' ? (HUMAN_KIND_RU[full.kind] ?? '') : '';
}

/** Основной текст карточки, который окно рисует отдельным блоком (ответ сессии, сообщение, команда). */
export function mainText(item: OwnerItem): string {
  const full = item.full;
  const pick = (key: string): string => (typeof full[key] === 'string' ? (full[key] as string) : '');
  if (item.kind === 'turn') return pick('answer');
  if (item.kind === 'agent_msg') return pick('text');
  if (item.kind === 'permission') return pick('input_text');
  return '';
}

/** Основной текст показывается, если его ещё нет в `brief` целиком (короткая команда уже видна в строке). */
export function mainShown(item: OwnerItem): boolean {
  const main = mainText(item).trim();
  return main !== '' && !(item.brief ?? '').includes(main);
}

/** Есть ли что показать под «развернуть» сверх `brief`. */
export function hasFull(item: OwnerItem, nowMs: number): boolean {
  if (mainShown(item)) return true;
  if (typeof item.full.reason === 'string' && item.full.reason !== '' && FULL_FIELDS[item.kind] === REQUEST_FIELDS) {
    return true;
  }
  return fullFields(item, nowMs).length > 0;
}

/** Длина показываемого `full` (для выбора «внутри карточки / окном»). */
export function fullLength(item: OwnerItem, nowMs: number): number {
  const reason = typeof item.full.reason === 'string' ? item.full.reason.length : 0;
  return fullFields(item, nowMs).reduce((n, f) => n + f.label.length + f.value.length, (mainShown(item) ? mainText(item).length : 0) + reason);
}

export interface OwnerQuestion {
  n: number;
  question: string;
  shown: string;
  options: string[];
  multi: boolean;
}

/** Вопросы карточки `question` (`full.questions`): `{n, question, shown, options, multi}`. */
export function ownerQuestions(full: Record<string, unknown>): OwnerQuestion[] {
  const raw = full.questions;
  if (!Array.isArray(raw)) return [];
  const out: OwnerQuestion[] = [];
  raw.forEach((q, index) => {
    if (q === null || typeof q !== 'object') return;
    const r = q as Record<string, unknown>;
    if (typeof r.question !== 'string') return;
    out.push({
      n: typeof r.n === 'number' ? r.n : index + 1,
      question: r.question,
      shown: typeof r.shown === 'string' ? r.shown : r.question,
      options: Array.isArray(r.options) ? r.options.filter((o): o is string => typeof o === 'string') : [],
      multi: r.multi === true,
    });
  });
  return out;
}

/** Ключ ответа вопроса в `answers` — текст вопроса как есть (`question`); контракт ядра `{вопрос: ответ}`. */
export function questionKey(q: OwnerQuestion): string {
  return q.question;
}
