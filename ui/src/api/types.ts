// Формы ответов API `/api/v1` — выведены из `docs/architecture.md` (со всеми блоками «Поправки») И
// СВЕРЕНЫ с кодом (`src/hub/core/queries.py`, `routes/*.py`). Где спека и код разошлись — описано рядом с
// полем, верен код.

/** Ф8-3 (`docs/architecture.md`, A3–A5): режим беседы — «реагирую», «к сведению», «сводка раз в
 * день», «разбор моделью»; ручной режим хранится в `channel.attention` (`null` — авто). Эффективный режим может
 * быть ещё и `muted` (заглушено — беседа или проект). */
export type AttentionMode = 'react' | 'info';
export type EffectiveAttention = AttentionMode | 'muted';

/** Ф27-1 B: строка `GET /accounts/{id}/mention_people`. */
export interface MentionPerson {
  id: string;
  name: string;
  handle: string | null;
}

export interface Sender {
  id: number;
  name: string | null;
  handle: string | null;
}

/** Сводка треда (спека `docs/architecture.md`): у сообщения — не null только у
 * родителя, у которого уже есть строка треда. `reply_count` — реальный счётчик ядра, `source_reply_count` —
 * то, что заявил источник (Slack ThreadInfo), может расходиться (ядро ещё не догрузило все ответы). */
export interface ThreadSummary {
  reply_count: number;
  source_reply_count: number | null;
  last_reply_ts: number | null;
  unread: number;
  subscribed: boolean;
}

/** Ф8-5 A3: вложение со статусом загрузки (`queries._message_row_to_dict` -> `media.attach_media`). `idx` —
 * позиция в `attachments_json`; `detected` — тип по сигнатуре байтов (показ выбирается по нему, не по `kind`,
 * П15); служебной ссылки источника здесь нет никогда. */
export type MediaStatus = 'deferred' | 'pending' | 'fetching' | 'ready' | 'failed' | 'gone' | 'too_big';

/** Ф14-6 C: расшифровка голосового вложения. `text` — последний удачный текст (при повторе остаётся до
 * нового); `problem` — сомнительный результат («пусто» — речи не слышно). Текст — чужие слова: только
 * простым текстом. */
export type TranscriptStatus = 'queued' | 'working' | 'done' | 'failed';

export interface Transcript {
  status: TranscriptStatus;
  text: string | null;
  lang: string | null;
  problem: string | null;
  manual: boolean;
}

export interface MediaItem {
  id: number;
  idx: number;
  kind: string;
  status: MediaStatus;
  mime: string | null;
  size: number | null;
  name: string | null;
  width: number | null;
  height: number | null;
  duration_s: number | null;
  detected: string | null;
  /** Ф14-6: необязательное — ядро старее 14-6 его не отдаёт. */
  transcript?: Transcript | null;
}

/** Строка ленты. `outbox_status` — не из спеки-текста напрямую, но есть в коде (`queries._message_row_to_dict`):
 * статус исходящего, JOIN с `outbox`, если у строки есть `outbox_id`; иначе `null`. Поля треда (спека 2b,
 * раздел E) — НЕОБЯЗАТЕЛЬНЫЕ ради сборки vue-tsc, сервер отдаёт их всегда. */
export interface Message {
  id: number;
  seq: number;
  first_seq: number;
  channel_id: number;
  project_id: number | null;
  ext_id: string | null;
  thread_ext_id: string | null;
  thread_broadcast?: boolean;
  purged?: boolean;
  thread?: ThreadSummary | null;
  reply_to_ext_id: string | null;
  grouped_id: string | null;
  is_me: boolean;
  kind: 'text' | 'event' | 'file';
  text: string | null; // null при deleted
  entities_json: string | null;
  attachments_json: string | null;
  forward_json: string | null;
  /** Ф11-8: адресаты (To+Cc) своего письма — JSON-список; у прочих сообщений `null`. */
  mail_to_json?: string | null;
  mentions_me: boolean;
  /** Ф20-1 B2: упоминание меня — только `@channel`/`@here`/`@everyone` (не личное). */
  mentions_broadcast?: boolean;
  /** Ф20-1 A3: отправитель — бот (метка «бот» у имени). */
  is_bot?: boolean;
  refs_json: string | null;
  reactions_json: string | null;
  /** Ф15-1 A5: письмо, на которое можно поставить реакцию (адресаты на Gmail, ящик с отправкой); только почта. */
  can_react?: boolean;
  ts: number;
  edited_ts: number | null;
  deleted: boolean;
  outbox_id: number | null;
  outbox_status: OutboxStatus | null;
  /** Ф37-1 C1: своё сообщение ушло без подтверждения, от сессии с этой ролью — подпись «агент: <роль>». */
  outbox_auto_role?: string | null;
  /** Ф41-5: то же, что `OutboxListItem.auto_session` (если ядро несёт его в сообщении). */
  outbox_auto_session?: number | null;
  /** Ф39 C: роль сессии, положившей черновик («сисадмин», «маркетолог 2»); `null` — роли нет. */
  outbox_author_role?: string | null;
  /** Ф12-2 A2: исходящее — черновик по графику; пока оно в очереди — когда уйдёт (`not_before_ts`). */
  outbox_paced?: boolean;
  outbox_not_before_ts?: number | null;
  /** Ф30-1 A2: черновик сессии «к точному времени» — когда отправить (мс), у прочих `null`. */
  outbox_send_at_ts?: number | null;
  /** Ф30-2 A2: догон к сроку не уйдёт, если после подтверждения в беседе ответят. */
  outbox_cancel_if_reply?: boolean;
  /** Ф69-1: очередь держит стоп первых сообщений новым людям. */
  outbox_held?: 'peer_flood' | null;
  /** Ф27-1: `{id: имя}` для `<@U…>` текста своей строки Slack с `outbox_id` (черновик, очередь, до эха). */
  mention_names?: Record<string, string>;
  sender: Sender | null;
  // Ф8-3 A4/A5: для меня ли (упоминание или ответ в подписанном треде) и внимание к сообщению. Необязательные
  // ради сборки vue-tsc/старого ядра, сервер Ф8-3 отдаёт их во всех выдачах сообщений.
  for_me?: boolean;
  attention?: EffectiveAttention;
  /** Ф63-1: id сработавших правил входящих проекта и правило внимания (`/sync`, лента); `rules` — с названиями. */
  rule_hits?: number[];
  rule_attention?: 'react' | 'info' | null;
  rules?: { id: number; title: string | null }[];
  /** Ф8-5 A3 — необязательное ради сборки тестовых фикстур, сервер отдаёт всегда. */
  media?: MediaItem[];
  /** Ф8-4: удалено владельцем у себя в хабе. В ленты такое не попадает; приходит только родителем треда
   * (заглушка «удалено у себя» в панели треда, П6) и заглушкой `/sync` (`HiddenChange`, строку убрать). */
  hidden?: boolean;
  /** Ф8-4 C3/П12: число ПРЕЖНИХ версий правленого сообщения (`GET /messages/{id}/edits`). */
  edit_count?: number;
  /** Ф14-8 A2: ждущая или неудавшаяся правка / «удалить у всех» своего сообщения; `null` — нет. */
  op?: MessageOp | null;
  /** Ф23-3: просмотры поста канала Telegram; не пост канала или ещё не снимали — `null`. */
  views?: PostViews | null;
  /** Ф44-1a: инлайн-кнопки под сообщением бота Telegram; кнопок нет — `null`. Адрес кнопки —
   * `(id сообщения, ряд, место)` с 1. Сырого callback в выдаче нет. Тексты и адреса — чужой ввод. */
  buttons?: MessageButtons | null;
}

/** Ф44-1a: вид кнопки. `callback` — действие у бота; `url` — ссылка (`u`); `copy` — текст для копирования
 * (`c`); `other` — только в приложении Telegram (web_view, url_auth, оплата, игра…). */
export type MessageButtonKind = 'callback' | 'url' | 'copy' | 'other';

export interface MessageButton {
  /** Текст кнопки. */
  t: string;
  k: MessageButtonKind;
  /** Адрес — только у `url`. */
  u?: string;
  /** Текст для копирования — только у `copy`. */
  c?: string;
  /** `callback` требует пароль Telegram — не нажимается из хаба. */
  pw?: true;
}

export interface MessageButtons {
  kind: 'inline';
  rows: MessageButton[][];
  /** Клавиатура обрезана по пределам (часть кнопок или все не показаны). */
  cut?: true;
}

/** Ф23-3: счётчики поста канала на момент снятия (`ts`, мс). */
export interface PostViews {
  views: number;
  forwards: number | null;
  ts: number | null;
}

/** Ф14-8: код неудачи операции у источника. */
export type MessageOpCode = 'expired' | 'forbidden' | 'not_found' | 'too_long' | 'other';

/** Ф14-8 A2: операция над своим сообщением (`msg_op`): `text` — только у правки. */
export interface MessageOp {
  kind: 'edit' | 'delete';
  state: 'queued' | 'failed';
  text?: string | null;
  error: { code: MessageOpCode | string | null; detail: string | null } | null;
}

/** Ф8-4 C3: одна прежняя версия сообщения — от старой к новой; `replaced_ts` — когда её сменила правка. */
export interface MessageEdit {
  replaced_ts: number;
  text: string | null;
  entities_json: string | null;
}

export interface MessageEditsResponse {
  message_id: number;
  edits: MessageEdit[];
}

export type OutboxStatus =
  | 'pending_approval'
  | 'queued'
  | 'sending'
  | 'sent'
  | 'failed'
  | 'unknown'
  | 'cancelled';

export interface Project {
  id: number;
  name: string | null;
  color: string | null;
  sort: number | null;
  muted: boolean;
  workdir: string | null;
  unread: number;
  // Ф8-3 A5: непрочитанные, требующие реакции (красный счётчик), и ключ `attention` политики проекта
  // (у псевдо-проектов `null`). Необязательные — старое ядро их не отдаёт.
  unread_react?: number;
  attention_policy?: Record<string, string> | null;
  /** Ф17-1b: вступление сессий в группы Telegram — с подтверждением или сразу. Необязательное — старое ядро. */
  session_join?: 'confirm' | 'auto';
  /** Ф37-3: блок «Разобрано сессией» у проекта включён. Необязательное — старое ядро. */
  session_handled?: boolean;
  /** Ф48-4: сообщения агентов подпискам проекта — пачкой раз в N минут (0 — сразу). Необязательное — старое ядро. */
  agent_batch_min?: number;
  /** Ф52-2: аккаунты, чьё «Неразобранное» слышат сессии проекта (только вход владельца). Необязательное — старое ядро. */
  unsorted_accounts?: number[];
  /** Ф41-2б-2: подписки сессий проекта включены (`hub project set N --subs on`). Необязательное — старое ядро. */
  subs?: boolean;
  /** Ф64-1: проект «по времени» (рабочий день кнопками). Необязательное — старое ядро. */
  workday?: boolean;
  /** Ф86-1: сводка дня подаётся сессиям хаба после сжатия и в начале новой (нет ключа — вкл). Старое ядро не шлёт. */
  digest_after_compact?: boolean;
  /** Ф86-1: слова владельца из соседних сессий проекта подаются сессиям хаба (нет ключа — вкл). Старое ядро не шлёт. */
  sibling_says?: boolean;
  /** Ф64-1: день открыт (у проекта не «по времени» всегда `true`). Необязательное — старое ядро. */
  day_open?: boolean;
  /** Ф64-1: время последней смены дня, мс; `null` — день ни разу не меняли. */
  day_ts?: number | null;
  /** Ф66-1: кто отвечает на карточки проекта, по группам: `me` | `agent` | `auto`. Только вход владельца; старое ядро поля не шлёт. */
  card_route?: Record<string, CardRoute>;
  /** Ф83: запасной движок сессий проекта по умолчанию; `null` или нет поля — не задан. */
  session_fallback?: FallbackChoice | null;
}

export type CardRoute = 'me' | 'agent' | 'auto';

export const UNSORTED_PROJECT_ID = -1;
export const ALL_PROJECTS_ID = -2;

export type ChannelKind =
  | 'dm'
  | 'group'
  | 'channel'
  | 'topic'
  | 'mail_thread'
  | 'issue'
  | 'card'
  | 'task';

/** Список каналов проекта (`GET /projects/{id}/channels`). */
export interface Channel {
  id: number;
  ext_id: string;
  kind: ChannelKind;
  title: string | null;
  tier: string | null;
  muted: boolean;
  /** Ф99-2: заглушила сессия (`hub_mute`) — её роль и причина; `null` — владелец или не заглушена. Старое ядро не отдаёт. */
  muted_by_role?: string | null;
  muted_by_note?: string | null;
  archived: boolean;
  map_reason: string | null;
  last_msg_ts: number | null;
  my_msg_count: number;
  first_seen_ts: number | null;
  unread: number;
  // Раздел E спеки 2b: разбивка `unread` на верхний уровень и подписанные треды. Необязательные ради
  // сборки vue-tsc, сервер отдаёт их всегда (list_channels, list_inbox, /sync — везде рядом с `unread`).
  unread_top?: number;
  unread_threads?: number;
  /** Ф66-5: время последней активности беседы = max(`last_msg_ts`, последний ответ любого треда) — по нему список. */
  last_activity_ts?: number | null;
  /** Ф66-5: число тредов с чужими ответами новее маркера прочтения треда (метка «свежий тред», не непрочитанное). */
  fresh_threads?: number;
  /** Ф66-5: `ext_id` родителя самого свежего такого треда. */
  fresh_thread_ext?: string | null;
  project_id: number | null;
  // Только в изменениях `/sync`/`/stream` (queries._channel_change_dict) — отсутствуют в списке каналов.
  seq?: number;
  last_read_seq?: number | null;
  // Спека 1d-1, п.5: НАОБОРОТ — есть только в `GET /projects/{id}/channels` (queries.list_channels), в
  // изменениях `/sync`/`/stream` (queries._channel_change_dict) поля нет вовсе. Название форума-родителя для
  // `kind='topic'`, у остальных `null`. Опционально, чтобы `applySync` могла отличить «неизвестно» (сама
  // ручка `/sync` этого не прислала — оставить как было) от «точно нет родителя» (`null`, из списка каналов).
  parent_title?: string | null;
  // Спека 2e, раздел «Ядро»: источник аккаунта (`telegram`/`slack`/`fake`/…) — решает семантику «Ответить»
  // (Slack -> тред, остальные -> reply_to). Необязательное ради сборки vue-tsc/на случай старого ядра в
  // rolling-деплое (как `unread_top`/`parent_title` выше); отсутствует -> UI ведёт себя как для НЕ-Slack.
  source?: string;
  /** Ф62-6: аккаунт беседы (нужен окну участников группы WhatsApp). */
  account_id?: number;
  // Ф8-3 A5/П15: эффективный режим, ручной (`null` — авто) и режим без ручного (подпись «Авто (сейчас: …)»);
  // `unread_react` — непрочитанные, требующие реакции (красный счётчик). Везде: список, `/sync`, один канал.
  attention?: EffectiveAttention;
  attention_manual?: AttentionMode | null;
  attention_auto?: EffectiveAttention;
  unread_react?: number;
  /** Ф34-1 A3: стадия беседы (свободная строка до 40 символов), заметка к ней (до 200) и время постановки (мс);
   * нет стадии — все три `null`. Список, один канал, `/sync`. Нет полей (старое ядро) — как «без стадии». */
  stage?: string | null;
  stage_note?: string | null;
  stage_ts?: number | null;
  /** Ф8-4 B3/П2: беседа удалена владельцем в хабе и нового после удаления не приходило — в `/sync` приходит с
   * `hidden: true`, стор убирает её из списка (открытая — закрывается). */
  hidden?: boolean;
  /** Ф10-5 B3: `false` — беседа уведомлений трекера (`attrs.no_reply`), ответа из хаба нет: вместо поля ввода
   * строка и ссылка в трекер. Нет поля (старое ядро) — поле ввода как раньше. Список, `/sync`, один канал. */
  can_reply?: boolean;
  /** Ф10-5 B3: задача Jira / карточка Trello; `url` — всегда `https://` (ядро иначе отдаёт `null`). */
  tracker?: ChannelTracker | null;
  /** Ф12-1 A2: беседа-заготовка черновика новому адресату (пока не ушло первое сообщение): адресат и
   * `can_reply=false` с причиной. После отправки поля нет. Ф12-5 A2: `can_reply` у заготовки — истина, пока
   * у неё нет живого первого сообщения (пустая или прошлое не ушло), причина — текстом ядра. */
  new_to?: NewRecipient | null;
  can_reply_reason?: string | null;
  /** Ф11-7: собеседник прочитал мои сообщения до этой позиции (Telegram: id сообщения); `null` — источник не
   * сообщал (Slack, почта). Список, `/sync`, один канал. */
  peer_read_sort?: number | null;
  /** Ф14-1 A6: `auto` — беседу раскладывают правила, `manual` — перенесена вручную (правила её не двигают).
   * Нет поля (старое ядро) — UI судит по `map_reason === 'manual'`. */
  map_source?: 'auto' | 'manual';
  /** Ф37-1 A1: роли сессий, которым беседа разрешена без подтверждения (пусто — нет). Список, `/sync`, один канал. */
  autosend?: string[];
}

/** Ф37-1 A2: ответ `PUT /channels/{id}/autosend`. */
export interface AutosendResult {
  autosend: string[];
  changed: boolean;
}

/** Ф12-1: адресат беседы-заготовки — Telegram по @username или новое письмо. Ф14-2 A1: Telegram по id
 * пользователя (`tg_user`) и 1–8 людей Slack (`slack_users`). Неизвестный вид (новое ядро) UI не рисует. */
export type NewRecipient =
  | { kind: 'tg_username'; username: string }
  | { kind: 'email'; to: string; subject: string; from: string | null }
  | { kind: 'tg_user'; user_id: number; name: string | null; username: string | null }
  | { kind: 'slack_users'; users: string[]; names: string[] }
  /** Ф40-4: 1–7 людей Mattermost (групповая личка). */
  | { kind: 'mm_users'; users: string[]; names: string[] }
  /** Ф34-3: Telegram по номеру телефона (полный номер видит только владелец при подтверждении; в списке — маска из `title`). */
  | { kind: 'tg_phone'; phone: string }
  /** Ф46-1: личка автору сообщения группы Telegram; `from_*` — только для показа и ссылки «к сообщению». */
  | {
      kind: 'tg_msg_author';
      name: string;
      from_title?: string;
      from_channel_id?: number;
      from_message_id?: number;
    };

/** Ф14-2 A2: адресат `POST /channels/new` — строка (Telegram `@username`, адрес почты) или объект. */
export type NewChannelTo =
  | string
  | { kind: 'tg_user'; user_id: number; name?: string | null; username?: string | null }
  | { kind: 'slack_users'; users: string[] }
  | { kind: 'mm_users'; users: string[] }
  /** Ф46-1: автор сообщения группы Telegram (id сообщения хаба); остальное выводит ядро. */
  | { kind: 'tg_msg_author'; message_id: number };

/** Ф14-2 A5: `GET /recipients?q=` — беседа, совпавшая по названию (выбор открывает её). */
export interface RecipientChannel {
  type: 'channel';
  channel_id: number;
  title: string | null;
  account_id: number;
  source: string;
  kind: string;
  project_id: number | null;
  last_msg_ts: number | null;
}

/** Ф14-2 A5: человек (контакт аккаунта или справочник); `to` — готовый адресат для `channels/new`. */
export interface RecipientPerson {
  type: 'person';
  account_id: number;
  source: string;
  name: string | null;
  handle: string | null;
  to: NewChannelTo;
  channel_id: number | null;
}

export type RecipientHit = RecipientChannel | RecipientPerson;

export interface ChannelTracker {
  name: 'jira' | 'trello' | 'notify';
  key: string;
  url: string;
}

export interface MessagePage {
  messages: Message[];
  next_before: string | null;
}

/** `GET /channels/{id}/threads/{thread_ext_id}/messages` (спека 2b, раздел E): ответы треда страницами,
 * родитель — отдельным полем (не внутри `messages`), `null` — «исходное сообщение не загружено». */
export interface ThreadMessagePage {
  messages: Message[];
  next_before: string | null;
  parent: Message | null;
}

/** `GET /channels/{id}/threads?unread=&limit=` (спека 2b, раздел E) — один тред канала: свежие первыми.
 * `parent: null` — родитель ещё не загружен в ядро (нужна заглушка «сообщение не загружено», спека 2e). */
export interface ThreadListItem {
  thread_ext_id: string;
  thread: ThreadSummary;
  parent: Message | null;
}

export interface InboxItem {
  id: number;
  project_id: number | null;
  last_msg_ts: number | null;
  ext_id: string;
  kind: ChannelKind;
  title: string | null;
  tier: string | null;
  muted: boolean;
  unread: number;
  unread_top?: number;
  unread_threads?: number;
}

export interface SearchResult {
  message_id: number;
  channel_id: number;
  ts: number;
  ext_sort: number;
  text: string | null;
  project_id: number | null;
  channel_title: string | null;
  is_me: boolean;
  thread_ext_id?: string | null;
  thread_broadcast?: boolean;
  purged?: boolean;
}

/** `GET /outbox?status=&limit=` (добавлена архитектором во время приёмки 6b, F8): очередь одного статуса,
 * новые сверху. Только `pending_approval | unknown | failed` — любой другой статус 422 на сервере. */
export type OutboxQueueStatus = 'pending_approval' | 'unknown' | 'failed';

/** Ф8-6 A2: статусы списка `/outbox` — очереди верхней полосы и `sent` («Отправлено сегодня» главной);
 * Ф12-2: `queued` — подтверждённое в очереди (запланированное по графику). */
export type OutboxListStatus = OutboxQueueStatus | 'sent' | 'queued';

export type BotGuardCategory = 'payment' | 'secret' | 'delete' | 'pdn' | 'file';

export interface OutboxListItem {
  id: number;
  /** Ф63-2: черновик в беседе с ботом Telegram — страховка сработала бы сейчас (оплата, секрет, удаление, ПДн, файл). */
  bot_guard?: BotGuardCategory | null;
  channel_id: number;
  project_id: number | null;
  channel_title: string | null;
  text: string | null;
  status: OutboxListStatus;
  origin: 'ui' | 'api';
  error: string | null;
  attempts: number;
  created_ts: number;
  sent_ts: number | null;
  ext_id: string | null;
  // Ф8-6 A2: адрес черновика и автор. Необязательные — старое ядро их не отдаёт.
  project_name?: string | null;
  source?: string;
  kind?: ChannelKind;
  token_name?: string | null;
  /** «ты» (UI), «Claude · <проект>» (токен hub-wake) или имя токена. */
  author?: string;
  reply_to?: { ext_id: string; sender: string | null; excerpt: string | null } | null;
  thread?: { ext_id: string; excerpt: string | null } | null;
  /** Запуск Claude, в котором черновик положен (`/agent/runs`). */
  run_id?: number | null;
  /** Ф12-1 C2: черновик новому адресату — «кому» (пока беседа — заготовка). */
  new_to?: NewRecipient | null;
  /** Ф12-2 A2: черновик по графику; у строки в очереди — когда уйдёт. */
  paced?: boolean;
  not_before_ts?: number | null;
  /** Ф30-1 A2: черновик «к точному времени» — когда отправить (мс), у прочих `null`. */
  send_at_ts?: number | null;
  /** Ф37-1 C2: строка ушла без подтверждения, от сессии с этой ролью («Отправлено сегодня»). */
  auto_role?: string | null;
  /** Ф41-5: не `null` — ушло веткой выключателя «автопилот» (id сессии хаба); подпись «без подтверждения · <роль> · автопилот». */
  auto_session?: number | null;
  /** Ф39 C: роль сессии, положившей черновик. */
  author_role?: string | null;
  /** Ф30-2 A2: догон к сроку не уйдёт, если после подтверждения в беседе ответят. */
  cancel_if_reply?: boolean;
  account_id?: number;
  /** Ф27-1: `{id: имя}` для `<@U…>` текста черновика Slack. */
  mention_names?: Record<string, string>;
  /** Ф14-3 A9: файлы черновика (строки `media` заготовки в порядке вложений); без файлов — `[]`. */
  files?: OutboxFile[];
}

/** Ф14-3 A9: файл черновика — смотреть через `GET /media/{media_id}`. Имя — недоверенный ввод. */
export interface OutboxFile {
  media_id: number;
  name: string | null;
  size: number | null;
  kind: string;
  mime: string | null;
}

/** Ф12-2 A4: `POST /outbox/approve-batch` — итог по каждому id в порядке запроса. */
export interface ApproveBatchResult {
  id: number;
  status: 'queued' | 'error';
  paced?: boolean;
  not_before_ts?: number;
  /** Ф30-1: у черновика «к точному времени» — его время; у ошибки `send_at_passed` — тоже. */
  send_at_ts?: number;
  error?: string;
  /** Ф30-1 П8: текст ошибки `send_at_passed` («время отправки ЧЧ:ММ прошло»). */
  message?: string;
}

/** Ф8-6 A3: `GET /home/react` — беседа, где ждут реакции. */
export interface HomeReactItem {
  channel: Channel;
  project: { id: number; name: string | null; color: string | null };
  last: {
    sender: string | null;
    excerpt: string | null;
    ts: number;
    message_id: number;
    /** Ф66-5: `ext_id` родителя, если последнее сообщение — ответ в треде. */
    thread_ext_id?: string | null;
  };
  card: { id: number; label: string; summary: string | null } | null;
  draft_count: number;
  /** Ф63-1: номера правил входящих последнего сообщения карточки и их названия (`null` — правило удалено/чужое). */
  rule_hits?: number[];
  rules?: { id: number; title: string | null }[];
  /** Ф34-2: у строк-напоминаний — строка напоминания (и тогда `last`/`card` в ответе `null`); у бесед `null`.
   * Стор (`loadHome`) строки-напоминания в `home.react` не кладёт — они живут в `state.reminders`. */
  reminder?: Reminder | null;
}

/** П1: `work`/`waiting`/`stopped` — запас под рабочие сессии Ф9 (в этом куске их никто не пишет). */
export type AgentRunKind = 'urgent' | 'reply' | 'digest' | 'work';
export type AgentRunStatus = 'running' | 'waiting' | 'ok' | 'failed' | 'paused' | 'retry' | 'stopped';

/** Ф8-6 A1: `GET /agent/runs` — запуск Claude по проекту. */
export interface AgentRun {
  id: number;
  project_id: number;
  project_name: string | null;
  project_color: string | null;
  kind: AgentRunKind;
  status: AgentRunStatus;
  started_ts: number;
  finished_ts: number | null;
  card_ids: number[];
  channels: { id: number; title: string | null }[];
  channel_titles: (string | null)[];
  draft_ids: number[];
  result: string | null;
  failure: string | null;
  session_id: string | null;
  num_turns: number | null;
  duration_ms: number | null;
}

/** Ф14-5 D/П9: строка подписки сессии на беседу из `GET /subs` (поле `channels`). */
export interface ChanSub {
  id: number;
  channel_id: number;
  project_id: number | null;
  session_name: string | null;
  session_pid: number;
  live: boolean;
  listening: boolean;
  waiting: boolean;
  ended_ts: number | null;
  /** Ф39 C: роль сессии-подписчика и её номер (`role_no > 1` — «роль 2»); роли нет — оба `null`. */
  role?: string | null;
  role_no?: number | null;
  /** Ф41-2а: подписка сессии хаба (см. `ProjectSub`). */
  held?: boolean;
  hub_session_id?: number;
  state?: HeldState;
}

/** Ф13-1/Ф14-5: `GET /subs` — подписки на проекты (`items`) и на беседы (`channels`). */
/** Ф41-6б (раздел 5 п.3): линия роли «не в сети» — последняя подписка кончилась не раньше 7 суток назад. */
export interface OfflineLine {
  key: string;
  project_id: number;
  role: string | null;
  role_no: number | null;
  engine: EngineName;
  session_name: string | null;
  cwd: string | null;
  ended_ts: number | null;
  end_reason: string | null;
  pending: number;
  sub_id: number;
}

/** Ф41-6б (раздел 5 п.2): расход сессий хаба за сегодня; поля может не быть (ядро старее). */
export interface SessionsToday {
  cost_usd: number;
  turns: number;
}

export interface SubsResponse {
  items: ProjectSub[];
  channels?: ChanSub[];
}

export interface OutboxItem {
  /** Ф27-1: см. `OutboxListItem.mention_names`. */
  mention_names?: Record<string, string>;
  id: number;
  channel_id: number;
  project_id: number | null;
  text: string;
  status: OutboxStatus;
  origin: 'ui' | 'api';
  error: string | null;
  attempts: number;
  created_ts: number;
  sent_ts: number | null;
  ext_id: string | null;
  /** Ф69-1: `peer_flood` — строка в очереди держится стопом первых сообщений новым людям. */
  held?: 'peer_flood' | null;
}

export type AccountStatus = 'ok' | 'stale' | 'auth_required' | 'limited' | 'no_network';

export interface Account {
  id: number;
  source: string;
  title: string | null;
  status: AccountStatus;
  last_ok_ts: number | null;
  last_error: string | null;
  flood_until: number | null;
  /** Ф69-1: стоп первых сообщений новым людям (PeerFlood) с этого момента (мс); `null` — стопа нет. Стоп всей
   * отправки — `status === 'limited'` и `last_error === 'PeerFlood'`. */
  peer_flood_ts?: number | null;
  /** Ф12-2 A5: сколько по графику на сегодня и суточный лимит графика аккаунта. */
  paced_today?: number;
  pace_daily?: number;
  /** Ф12-5 A6 (только UI-сессии): с этого аккаунта можно «Новое сообщение» (Telegram и почта, кроме ящика
   * «только на приём»); `address` — адрес ящика почты, прочим `null`. */
  new_message?: boolean;
  address?: string | null;
  /** Ф34-3: лимит поисков Telegram по номеру в сутки (только Telegram); 0 — поиск по номеру выключен. */
  tg_phone_daily?: number;
  /** Ф70-1: аккаунт целиком в проекте, чей день закрыт, — канал спит (приём остановлен, уходит только ручная
   * отправка). `null`/нет поля — не спит. */
  sleep?: { project_id: number; since_ts: number } | null;
}

/** Ф12-5 A1: `POST /channels/new` — беседа-заготовка (как `GET /channels/{id}`); `created: false` — отдана
 * уже открытая тому же адресату в том же проекте. */
export interface ChannelNewResult {
  channel: Channel;
  created: boolean;
}

export interface StatusResponse {
  accounts: Account[];
  dead_letter: Record<string, number>;
  outbox_pending: number; // pending_approval + queued + sending
  outbox_approval: number; // только pending_approval — счётчик «ждёт подтверждения»
  outbox_unknown: number;
  max_seq: number;
  now: number;
  /** Ф8-5 П12: только UI-сессии — объём медиа и свободное место. */
  media?: {
    bytes_by_account: Record<string, number>;
    total_bytes: number;
    pending: number;
    failed: number;
    free_bytes: number | null;
    line: string;
  };
  /** Ф16-1: движок сессий (чип в шапке). Необязательный — старое ядро поля не отдаёт, тогда «Claude Code». */
  engine?: EngineName;
  /** Ф78-2a: модель и усилие агента владельца (умолчание подставлено) — чип в меню. */
  engine_model?: string | null;
  engine_effort?: string | null;
  /** Ф74-1: профили движков сессий хаба (`engine/registry.py`). Старое ядро поля не отдаёт — тогда
   * `DEFAULT_SESSION_ENGINES` (`util/engineProfiles.ts`). */
  session_engines?: SessionEngineProfile[];
}

/** Ф74-6: модель из `models` профиля. */
export interface SessionEngineModel {
  id: string;
  efforts: string[];
  default_effort?: string | null;
}

/** Ф74-1: профиль движка сессий хаба из `/status`. `available: false` — заготовка, интерфейс её не предлагает. */
export interface SessionEngineProfile {
  name: string;
  label: string;
  short: string;
  efforts: string[];
  model_hints: string[];
  /** Ф74-6: живой список моделей движка (Codex — `model/list`) с усилием у каждой. Нет или пусто — `model_hints`/`efforts`. */
  models?: SessionEngineModel[];
  /** Ф74-6: ключи выключателей «Автопилота», которые на движке не действуют (у Codex — `cross_session`, `plan_mode`). */
  dead_switches?: string[];
  /** Ф74-8: у движка есть вопрос с вариантами (у Cursor в `-p` нет). Нет поля — есть. */
  asks_questions?: boolean;
  /** Ф74-8: реплика посреди хода входит только на ближайшем вызове инструмента (Cursor). */
  mid_turn_tool_only?: boolean;
  /** Ф74-8: движок сообщает состояния MCP-серверов (у Cursor нет). Нет поля — сообщает. */
  mcp_statuses?: boolean;
  compacts_self: boolean;
  context_known: boolean;
  mid_turn: boolean;
  available: boolean;
}

// --- Ф16-1: переключатель движка сессий (`src/hub/core/engine.py`, `core/routes/engine.py`) -----------------

// Ф71-1: `cursor` — только движок сессий хаба (глобальный переключатель — claude | opencode).
export type EngineName = 'claude' | 'opencode' | 'cursor' | 'codex';

/** Модель движка в `GET /engine` (Ф78-2a): `images` — видит картинки, `efforts` — усилия этой модели. */
export interface EngineModel {
  id: string;
  name: string | null;
  images: boolean | null;
  efforts: string[];
}

/** Один из четырёх движков в `GET /engine`; `model`/`effort` — выбор владельца, `null` — умолчание. */
export interface EngineInfo {
  name: EngineName;
  label: string;
  available: boolean;
  reason: string | null;
  models: EngineModel[];
  efforts: string[];
  default_model: string | null;
  default_effort: string | null;
  model: string | null;
  effort: string | null;
  /** Добавка 22.10: список и умолчание для фактов (у Claude есть `haiku`); старое ядро не присылает. */
  facts_models?: EngineModel[];
  facts_default_model?: string | null;
  facts_default_effort?: string | null;
}

/** Ф83: запасной выбор «движок · модель · усилие». */
export interface FallbackChoice {
  engine: EngineName;
  model: string | null;
  effort: string | null;
}

/** Ф83: настройка запасного агента/фактов в `GET /engine` (`ts` — когда задали). */
export interface FallbackSetting extends FallbackChoice {
  ts?: number | null;
}

/** Ф83: «сейчас на запасном» — основной выбор, повод (`limit | balance | auth | model`), причина, начало (мс). */
export interface OnFallback {
  main: FallbackChoice;
  kind: string;
  reason: string | null;
  since: number;
}

/** Блок `facts` (Ф78-2d): выбор службы фактов; `follows_agent` — своей записи нет, факты идут за агентом. */
export interface EngineFacts {
  engine: EngineName;
  model: string | null;
  effort: string | null;
  follows_agent: boolean;
  /** Ф83: запасной с подставленными умолчаниями, действующий выбор и пометка «на запасном»; старое ядро не шлёт. */
  fallback?: FallbackChoice | null;
  effective?: FallbackChoice;
  on_fallback?: OnFallback | null;
}

/** Ответ `GET /api/v1/engine` (Ф78-2a, Ф78-2d): выбор агента владельца сверху, четыре движка, блок фактов. */
export interface EngineStatus {
  engine: EngineName;
  model: string | null;
  effort: string | null;
  /** Когда переключали; `null` — не переключался. */
  changed_ts: number | null;
  engines: EngineInfo[];
  opencode_available?: boolean;
  opencode_reason?: string | null;
  /** Старое ядро без блока — факты идут за агентом. */
  facts?: EngineFacts;
  /** Ф83: сырые записи запасных (`null` — не задан); основной выбор остаётся в полях выше. */
  agent_fallback?: FallbackSetting | null;
  facts_fallback?: FallbackSetting | null;
  /** Ф83: запасной агента с умолчаниями, действующий выбор агента, пометка «на запасном». */
  fallback?: FallbackChoice | null;
  effective?: FallbackChoice;
  on_fallback?: OnFallback | null;
}

/** Ответ `PUT /api/v1/engine` — как `GET` плюс что было. */
export interface EngineSetResult extends EngineStatus {
  old_engine: EngineName;
}

/** Ответ `POST /messages/{id}/media/{idx}/fetch` (Ф8-5 A2.3, П8) и `POST /media/{id}/transcribe` (Ф14-6). */
export interface MediaFetchResponse {
  message_id: number;
  media: MediaItem[];
}

export interface SyncResponse {
  messages: Message[];
  channels: Channel[];
  max_seq: number;
  more: boolean;
  /** Ф41-2б-2: изменённые постоянные сессии хаба (без `listening`/`workdir`/`cache`) и их запросы. Необязательные — старое ядро. */
  hub_sessions?: HubSession[];
  session_requests?: SessionRequest[];
  /** Ф17-1b: изменённые заявки сессий на вступление в группы и каналы. Необязательное — старое ядро. */
  joins?: JoinRequest[];
  /** Ф17-2b: изменённые заявки на выход из групп и каналов Telegram. Необязательное — старое ядро. */
  leaves?: LeaveRequest[];
  /** Ф62-6: изменённые заявки на группы WhatsApp (создать, участники, ссылка). Необязательное — старое ядро. */
  group_ops?: GroupOp[];
  /** Ф34-2: изменённые напоминания по беседам (постановка, срабатывание, перенос, закрытие, автоснятие). */
  reminders?: Reminder[];
  /** Ф38-2: изменённые заявки сессий на правило раскладки. Необязательное — старое ядро. */
  rule_requests?: RuleRequest[];
  /** Ф41-5: изменённые заявки агентов (перенос беседы, напарник). Необязательное — старое ядро. */
  agent_requests?: AgentRequest[];
  /** Ф44-1d: изменённые нажатия кнопок ботов (с ответом бота) — только UI. Необязательное — старое ядро. */
  presses?: ButtonPress[];
  /** Ф54-1: заявки «нужен человек» (по возрастанию `seq`) — только UI. Необязательное — старое ядро. */
  human_requests?: HumanRequest[];
  /** Ф35-3: изменённые сообщения агентов (целыми строками) — только UI. Необязательное — старое ядро. */
  peer_msgs?: PeerMsg[];
}

// --- Запросы и события сессий хаба (Ф78-1: задач по «Го» больше нет) ---

export type WorkRequestStatus = 'pending' | 'allowed' | 'denied' | 'answered' | 'expired';

export type JoinStatus =
  | 'checking'
  | 'pending'
  | 'approved'
  | 'joined'
  | 'requested'
  | 'already'
  | 'failed'
  | 'declined';

/** Предпросмотр цели (`join.request_dict.peek`): пишут посторонние люди — только текстом. */
export interface JoinPeek {
  title: string | null;
  /** Вид чата по Telegram: `broadcast` — канал, `megagroup` — супергруппа, `group` — обычная группа. */
  kind: 'broadcast' | 'megagroup' | 'group' | null;
  participants: number | null;
  about: string | null;
  request_needed: boolean;
  member: boolean;
  forum: boolean;
  ext_id: string | null;
  /** Ф17-3: связанный чат — `discussion`: у канала это его группа обсуждения; `channel`: у группы — её канал. */
  linked?: { title: string; username: string | null; kind: 'discussion' | 'channel' } | null;
}

/** `join.request_dict` ядра: заявка сессии на вступление (Ф17-1). `reason`/`error` — от сессии и ядра. */
export interface JoinRequest {
  id: number;
  account_id: number;
  project_id: number;
  target_kind: string;
  target: string;
  reason: string | null;
  status: JoinStatus;
  auto: boolean;
  peek: JoinPeek | null;
  ext_id: string | null;
  title: string | null;
  channel_id: number | null;
  error: string | null;
  client_name: string | null;
  created_ts: number;
  decided_ts: number | null;
  done_ts: number | null;
  updated_ts: number;
  can_decline: boolean;
  joining: boolean;
  seq: number;
}

export type GroupOpKind = 'create' | 'add' | 'remove' | 'link' | 'link_reset' | 'members';
export type GroupOpStatus = 'pending' | 'approved' | 'done' | 'failed' | 'declined';
export type GroupOpCode =
  | 'ok'
  | 'privacy'
  | 'already'
  | 'not_member'
  | 'no_whatsapp'
  | 'no_pn'
  | 'self'
  | 'left_recently'
  | 'error'
  | 'bad';

/** Участник заявки: сырого номера нет, только маска; `code` — после `done`. */
export interface GroupOpParticipant {
  i: number;
  kind: 'phone' | 'chat' | 'jid';
  mask: string;
  name: string | null;
  channel_id: number | null;
  code?: GroupOpCode;
  invite?: boolean;
}

/** `group_op` ядра (Ф62-6): заявка на группу WhatsApp. `title`, `reason`, имена — чужой текст. */
export interface GroupOp {
  id: number;
  account_id: number;
  project_id: number | null;
  channel_id: number | null;
  kind: GroupOpKind;
  title: string | null;
  participants: GroupOpParticipant[];
  reason: string | null;
  origin: 'session' | 'ui';
  status: GroupOpStatus;
  error: string | null;
  link: string | null;
  client_name: string | null;
  role: string | null;
  created_ts: number;
  decided_ts: number | null;
  done_ts: number | null;
  updated_ts: number;
  can_decline: boolean;
  seq: number;
  auto?: boolean;
}

/** Участник группы WhatsApp (`GET /channels/{id}/group-members`, кука): `phone` — номер целиком. Имя — чужой текст. */
export interface GroupMember {
  jid: string | null;
  name: string | null;
  phone_masked: string | null;
  phone?: string | null;
  is_admin: boolean;
  is_me: boolean;
}

export interface GroupMembers {
  channel_id: number;
  members: GroupMember[];
  updated_ts: number | null;
  /** Открытое задание обновления состава; нет — `null`. */
  refresh: { op_id: number; status: GroupOpStatus } | null;
}

/** `POST /group-ops/ui`: участник — `+7…`/`8…`, `#<id лички WhatsApp>` или jid. */
export interface GroupOpInput {
  kind: GroupOpKind;
  projectId?: number | null;
  accountId?: number | null;
  channelId?: number | null;
  title?: string;
  participants?: string[];
}

export type LeaveStatus = 'pending' | 'approved' | 'left' | 'failed' | 'declined';

/** `leave.request_dict` ядра: заявка на выход из группы или канала Telegram (Ф17-2). `title`, `reason` — чужой текст. */
export interface LeaveRequest {
  id: number;
  account_id: number;
  project_id: number | null;
  /** У темы форума — сам форум. */
  channel_id: number;
  title: string | null;
  chat_kind: 'group' | 'channel';
  reason: string | null;
  origin: 'session' | 'ui';
  status: LeaveStatus;
  result: 'left' | 'not_member' | null;
  error: string | null;
  client_name: string | null;
  created_ts: number;
  decided_ts: number | null;
  done_ts: number | null;
  /** Ф41-5: принято автопилотом (без владельца). */
  auto?: boolean;
  /** Хаб вернул беседу после выхода (сообщение или вступление): выход больше не в силе. */
  returned_ts: number | null;
  updated_ts: number;
  can_decline: boolean;
  leaving: boolean;
  seq: number;
}

/** Ф41-5 (раздел 12 спеки): выключатель автопилота. `manual` — вид в ручном режиме; `meta` — снимает и остальные ограничения. */
export interface AutopilotSwitch {
  key: string;
  group: string;
  title: string;
  manual: 'ask' | 'deny';
  meta?: boolean;
  note?: string | null;
  /** Ф54-1: `session` — ключ сессии (все прежние), `project` — действует на заявки проекта. Старое ядро поля не шлёт. */
  scope?: 'session' | 'project';
}

/** `GET /autopilot`: `true` — стоит (придержано). `projects` — только переопределения; `local` — запрос с ноутбука
 * (справочно: с телефона меняется так же, 24.10). */
export interface AutopilotState {
  switches: AutopilotSwitch[];
  global: Record<string, boolean>;
  projects: Record<string, Record<string, boolean>>;
  local: boolean;
}

export type AgentRequestKind = 'move_channel' | 'new_session';

export interface AgentRequestPayload {
  channel_id?: number;
  channel_title?: string | null;
  from_project_id?: number | null;
  role?: string | null;
  task?: string | null;
  engine?: string | null;
  model?: string | null;
  effort?: string | null;
  mode?: string | null;
  /** Ф50-7: подпапка и файл роли, заданные агентом. */
  subdir?: string | null;
  role_file?: string | null;
}

/** Ф41-5: заявка агента (раздел 12). `who`, `reason`, `note` и поля `payload` — чужой ввод: только текстом. */
export interface AgentRequest {
  id: number;
  seq?: number;
  kind: AgentRequestKind;
  project_id: number | null;
  who: string | null;
  reason: string | null;
  status: 'pending' | 'approved' | 'declined';
  auto: boolean;
  note: string | null;
  created_ts: number;
  decided_ts: number | null;
  payload: AgentRequestPayload | null;
  /** Ф50-7: готовые тексты предупреждений ядра по папке и файлу роли (чужой ввод — только текстом). */
  subdir_warning?: string | null;
  role_file_warning?: string | null;
}

/** Ф44-1d: состояние нажатия кнопки бота (контракт 44-1c, 11.3). */
export type PressStatus = 'pending' | 'approved' | 'leased' | 'done' | 'failed' | 'unknown' | 'declined' | 'expired';

/** Ответ бота на нажатие — чужой текст: только строкой. */
export interface PressResult {
  message?: string;
  alert?: boolean;
  url?: string;
  timeout?: boolean;
}

/** Ф44-1d: строка нажатия (`press_dict` ядра). `role`, `channel_title`, `quote`, `text`, `result` — чужой ввод. */
export interface ButtonPress {
  id: number;
  account_id: number;
  channel_id: number;
  project_id: number | null;
  message_id: number;
  row: number;
  col: number;
  text: string;
  by: 'ui' | 'session';
  role: string | null;
  auto: boolean;
  auto_by: 'list' | 'autopilot' | null;
  status: PressStatus;
  reason: string | null;
  created_ts: number;
  decided_ts: number | null;
  done_ts: number | null;
  updated_ts: number;
  seq: number;
  /** Только у ждущей карточки. */
  channel_title?: string | null;
  quote?: string | null;
  result?: PressResult | null;
}

export type HumanKind = 'captcha' | 'login' | 'code' | 'payment' | 'consent' | 'other';
export type HumanStatus = 'pending' | 'done' | 'failed' | 'later' | 'expired' | 'cancelled';

/** Ф54-1: заявка «нужен человек» (`docs/architecture.md`). Тексты автора — чужой ввод. */
export interface HumanRequest {
  id: number;
  project_id: number;
  kind: HumanKind;
  site: string | null;
  url: string | null;
  doing: string | null;
  need: string;
  profile: string | null;
  tab: string | null;
  shot: { size: number; mime: string } | null;
  expires_ts: number | null;
  status: HumanStatus;
  decided_ts: number | null;
  /** Только владельцу: кто ответил (`bot` — запас). */
  decided_by?: 'ui' | 'agent' | 'bot' | null;
  /** Ф93-2: причина исхода — что написал решивший к «Готово» / «Не вышло» / «Не сейчас» (`null` — без причины). */
  decided_note?: string | null;
  /** Ф58-3, только владельцу: открытое поручение агенту по заявке (пока оно есть, кнопки владельца ядро не принимает). */
  errand?: OwnerErrand | null;
  /** Ф58-3, только владельцу: агент вернул последнее поручение — причина и время. */
  errand_returned?: OwnerErrandReturned | null;
  role: string;
  role_no: number;
  created_ts: number;
  seq: number;
}

export interface ButtonPressReply {
  press: ButtonPress;
  created: boolean;
}

export interface AgentRequestApproveBody {
  role?: string;
  engine?: string;
  model?: string;
  effort?: string;
  mode?: 'manual' | 'auto';
  want_sub?: SessionWantSub | null;
  autosend_ok?: boolean;
  /** Ф50-7: оба ключа шлются всегда; `""` — без папки / без файла. */
  subdir?: string;
  role_file?: string;
}

/** Ф41-8: `GET /roles` — роль каталога хаба. `sub`: `direct` — только сообщения агентов, `project` — входящее проекта. */
export interface RoleInfo {
  key: string;
  title: string;
  names: string[];
  sub: 'direct' | 'project';
}

/** `GET /roles/{key}`: `text` — чистый текст (markdown), показывать только текстом. */
export interface RoleText extends Omit<RoleInfo, 'sub'> {
  sub: 'direct' | 'project';
  text: string;
}

/** Ф48-3: `GET /sessions/{id}/role-text` — общий текст хаба и файл роли сессии (путь в проекте, содержимое или
 * причина, почему не прочитан). Показывать только текстом. */
export interface SessionRoleText {
  role: string | null;
  common: string;
  role_file: string | null;
  file_text: string | null;
  file_reason: string | null;
  /** Ф48-5: совпадает ли текущий файл роли (путь и текст) с закреплённым в системном тексте при создании
   * разговора Claude Code; `null` — снимка нет или движок другой. Старое ядро поля не отдаёт. */
  pinned?: boolean | null;
}

export interface HandoffInput {
  engine?: EngineName;
  model?: string | null;
  effort?: string | null;
  mode?: 'manual' | 'auto';
  /** Ф50-7: без ключа — папка прежней сессии; `""` — корень; строка — другая папка. */
  subdir?: string;
}

export type RuleRequestStatus = 'pending' | 'approved' | 'declined';

/** `rule_request.request_dict` ядра: заявка сессии на правило раскладки (Ф38-2). Текстовые поля — чужие. */
export interface RuleRequest {
  id: number;
  seq: number;
  status: RuleRequestStatus;
  project_id: number;
  account_id: number;
  account_source: string | null;
  /** П1: токену с ограниченным списком проектов не отдаётся. */
  account_title: string | null;
  kind: 'domain' | 'label' | 'folder' | 'all';
  value: string | number | null;
  description: string;
  reason: string | null;
  /** Имя агента: «маркетолог», «маркетолог 2». */
  who: string | null;
  created_ts: number;
  decided_ts: number | null;
  rule_id: number | null;
  /** Сколько бесед правило переложит (у `approved` — фактически переложено, П7). */
  moved: number | null;
  /** До 3 названий бесед, только у ждущих; П1: может быть `null`. */
  examples: string[] | null;
  /** Ф41-5: принято автопилотом (без владельца). */
  auto?: boolean;
}

export type WorkEventKind =
  | 'start'
  | 'tool'
  | 'tool_done'
  | 'text'
  | 'request'
  | 'decision'
  | 'say'
  | 'turn_end'
  | 'status'
  | 'error'
  | 'warn';

export interface WorkEvent {
  id: number;
  ts: number;
  kind: WorkEventKind;
  tool: string | null;
  summary: string;
  is_error: boolean;
  ref: string | null;
}

export type WorkSayPriority = 'next' | 'later' | 'now';

export interface AuthMe {
  token_id: number;
  kind: 'ui' | 'interactive' | 'background';
  scopes: string[];
  project_ids: number[] | null;
  csrf: string | null;
  /** Ф10-7: запрос пришёл на удалённый слушатель (телефон через туннель). Нет поля — старое ядро, локально. */
  remote?: boolean;
}

export interface SessionResponse {
  csrf: string;
  expires_ts: number;
}

/** Ф10-7: откуда вошла UI-сессия; `null` — строка до 25.09 (миграция 0019). */
export type SessionVia = 'local' | 'remote' | null;

/** Ф10-7: `GET /auth/sessions` — живые UI-сессии, по `last_used_ts` убыв., `null` в конце. */
export interface UiSession {
  id: number;
  via: SessionVia;
  created_ts: number | null;
  last_used_ts: number | null;
  expires_ts: number;
  user_agent: string | null;
  current: boolean;
}

/** Ф10-7: `GET /api/v1/remote` — состояние доступа с телефона. */
export interface RemoteStatus {
  enabled: boolean;
  hosts: string[];
  password_set: boolean;
  password_set_ts: number | null;
  /** Удалённый слушатель поднят (порт мог быть занят — ядро стартует без него). */
  listening: boolean;
  failures_24h: number;
  blocked_until: number | null;
  /** Ф14-9 решение 7: `null` — пары https нет, удалённый слушатель по http. */
  tls: RemoteTls | null;
}

/** Ф14-9: сертификат https удалённого слушателя (прочитан ядром на старте). `error` — пара не грузится,
 * удалённый вход не поднят; тогда срока и имён нет. */
export interface RemoteTls {
  not_after_ms: number | null;
  names: string[];
  error: string | null;
}

export interface RecentMessage {
  id: number;
  ts: number;
  is_me: boolean;
  text: string | null;
}

export interface ResolveCandidate {
  id: number;
  project_id: number | null;
  source: string;
  kind: ChannelKind;
  title: string | null;
  first_seen_ts: number | null;
  my_msg_count: number;
  title_history_json: string | null;
  recent_messages: RecentMessage[];
}

export interface SendResult {
  outbox_id: number;
  message_id: number;
  status: OutboxStatus;
  created: boolean;
}

/** Ф10-4 A2: ответ `POST /uploads?channel_id=` — файл загружен в ядро и привязан к беседе; отправка ссылается
 * на `upload_id` (`files: [upload_id]` в `POST /messages/send`). Тип — по сигнатуре байтов. */
export interface UploadResult {
  upload_id: string;
  size: number;
  mime: string;
  name: string;
  /** Ф14-4: `voice` — голосовое из записи (`POST /uploads?voice=1`). */
  kind: 'photo' | 'document' | 'voice';
}

export type ResolveUnknownDecision = 'sent' | 'not_sent' | 'retry';

// --- Ф14-1: раскладка и проекты из интерфейса (`docs/architecture.md`) ----------------------

/** Вид условия правила: `channel` (одна беседа) заводится только из CLI, `other` — вид, который UI не разбирает. */
export type MapRuleKind = 'folder' | 'label' | 'domain' | 'all' | 'channel' | 'other';

/** Строка `GET /map-rules`: порядок — `account_id`, затем порядок проверки (`priority DESC, id ASC`). */
export interface MapRule {
  id: number;
  account_id: number;
  project_id: number;
  priority: number;
  kind: MapRuleKind;
  value: number | string | null;
  description: string;
  /** Сколько бесед аккаунта сейчас разложено этим правилом. */
  channels: number;
}

/** Тело `POST /map-rules` (без `dry_run` — его ставит клиент). */
export interface MapRuleInput {
  account_id: number;
  project_id: number;
  kind: 'folder' | 'label' | 'domain' | 'all';
  value?: number | string | null;
  priority?: number | null;
}

export interface MapMoveExample {
  channel_id: number;
  title: string | null;
  from_project_id: number | null;
  to_project_id: number | null;
}

/** Предпросмотр (`dry_run`) добавления или удаления правила: ничего не записано. */
export interface MapPreview {
  dry_run: true;
  description: string;
  moved: number;
  examples: MapMoveExample[];
}

export interface MapRuleAdded {
  rule_id: number;
  channels_relaid: number;
  priority: number;
  description: string;
}

export interface MapRuleDeleted {
  ok: true;
  channels_relaid: number;
}

export interface MapOptionFolder {
  id: number;
  title: string;
  channels: number;
  unsorted: number;
}

export interface MapOptionLabel {
  name: string;
  channels: number;
  unsorted: number;
}

export interface MapOptionDomain {
  domain: string;
  channels: number;
  unsorted: number;
}

/** `GET /accounts/{id}/map-options`: доступные виды условий и значения со счётом бесед. */
export interface MapOptions {
  account_id: number;
  source: string;
  kinds: Array<'folder' | 'label' | 'domain' | 'all'>;
  folders: MapOptionFolder[];
  labels: MapOptionLabel[];
  domains: MapOptionDomain[];
}

/** Цель переноса: проект, «Неразобранное» (`null`) или назад к правилам (`auto`). */
export type MoveTarget = number | null | 'auto';

// --- Ф21-1b: люди (`core/routes/people.py`, спека `docs/architecture.md` и Р1–Р18) -------

/** Проект в карточке человека: название `null` у проектов не из токена (П6; у UI — всегда есть). */
export interface PersonProject {
  id: number;
  title: string | null;
}

export interface PersonSource {
  source: string;
  handle: string | null;
  email: string | null;
}

/** `PersonBrief` (E4/E6, с Р2: `projects` — `[{id, title}]`). */
export interface PersonBrief {
  id: number;
  name: string;
  known: boolean;
  sources: PersonSource[];
  projects: PersonProject[];
  last_ts: number | null;
  in_30d: number;
  out_30d: number;
  reply_me_median_ms: number | null;
  reply_them_median_ms: number | null;
}

export interface PeopleList {
  items: PersonBrief[];
  total: number;
}

export interface PersonSuggestion {
  a: PersonBrief;
  b: PersonBrief;
  reason: 'name' | 'handle';
}

export interface PersonContact {
  contact_id: number;
  account_id: number;
  source: string;
  account_title: string | null;
  name: string | null;
  handle: string | null;
  email: string | null;
  manual: boolean;
}

export interface PersonChannel {
  channel_id: number;
  title: string | null;
  kind: string;
  project_id: number | null;
  in: number;
  out: number;
  seen: number;
}

export interface PersonReply {
  median_ms: number;
  p75_ms: number;
  n: number;
  unanswered: number;
}

export interface PersonStyle {
  n: number;
  median_len: number;
  address: 'ты' | 'вы' | 'смешанно' | null;
  emoji_share: number;
  greet_share: number;
  lang: 'ru' | 'en' | 'mixed' | null;
}

export interface PersonReactionCount {
  key: string;
  n: number;
}

export interface PersonStats {
  in: { total: number; d30: number; personal: number; first_ts: number | null; last_ts: number | null };
  out: { total: number; d30: number; first_ts: number | null; last_ts: number | null };
  seen: { total: number; last_ts: number | null };
  reply_me: PersonReply;
  reply_them: PersonReply;
  style: { chat: PersonStyle | null; mail: PersonStyle | null };
  react: { me_to_them: PersonReactionCount[]; them_to_me: PersonReactionCount[] };
}

export interface PersonExample {
  message_id: number;
  channel_id: number;
  ts: number;
  kind: 'chat' | 'mail';
  text: string;
}

/** Ф24-1c: факт о человеке в карточке (`person_facts.card_facts`). */
export interface PersonFact {
  id: number;
  text: string;
  source: 'auto' | 'manual';
  updated_ts: number;
}

export interface PersonFacts {
  items: PersonFact[];
  count: number;
  updated_ts: number | null;
}

export interface PersonCard {
  id: number;
  name: string;
  name_manual: boolean;
  names: string[];
  known: boolean;
  contacts: PersonContact[];
  projects: PersonProject[];
  channels: PersonChannel[];
  stats: PersonStats | null;
  examples: PersonExample[];
  stats_ts: number | null;
  facts: PersonFacts;
}

// --- Ф34-2: напоминания по беседам (`core/reminders.py: reminder_dict`) ----------------------------------------

export type ReminderStatus = 'pending' | 'fired' | 'done' | 'cancelled' | 'answered';

/** Напоминание. `note` и `channel.title` — чужие данные (пишет сессия, название — собеседник): только текстом.
 * `project_id` — где поставлено (может отличаться от проекта беседы), `null` — «Неразобранное». */
export interface Reminder {
  id: number;
  /** Ф89-1: `null` — напоминание без беседы (тогда `channel` тоже `null`). */
  channel_id: number | null;
  project_id: number | null;
  due_ts: number;
  note: string | null;
  cancel_if_reply: boolean;
  status: ReminderStatus;
  /** `me` — поставила владелец, `session` — сессия (тогда `role` — её роль или `null`). */
  by: 'me' | 'session';
  role: string | null;
  created_ts: number;
  updated_ts: number;
  fired_ts: number | null;
  delivered_ts: number | null;
  closed_ts: number | null;
  end_reason: string | null;
  waiting: boolean;
  seq: number;
  channel: { id: number; title: string | null; project_id: number | null; archived: boolean } | null;
  /** Ф89-1: период — `day`/`weekdays`/`week`/`<N>h`; `null` — разовое. */
  every?: string | null;
  every_text?: string | null;
  at_hm?: string | null;
  tz?: string | null;
  /** Пропущено сроков подряд (повторяемое). */
  missed?: number;
  done_count?: number;
  last_done_ts?: number | null;
  /** Роль сессии, отметившей «сделано»; `null` — владелец. */
  last_done_role?: string | null;
  /** Только в `GET /reminders?counts=1`. */
  new_since_done?: number | null;
}

// --- Ф35-3: агенты хаба (`GET /subs?live=1`, `core/peer_msgs.py`) -------------------------------------------------

/** Ф41-1: срок кэша промпта и размер контекста сессии Claude Code (`core/transcript.py`); времена — мс. */
export interface SessionCache {
  last_ts: number;
  ttl_s: number;
  expires_ts: number;
  context_tokens: number;
  model: string;
}

export type SubMode = 'cards' | 'messages' | 'direct';

/** Ф77-1: машина роли (контракт A5 спеки `phase77-01-session-machine-spec.md`). `display` — что показывать в чипе. */
export interface Machine {
  /** 16 hex (хеш machine-id); `null` — машину ядра не назвать. */
  host: string | null;
  name: string | null;
  hostname: string | null;
  display: string;
  route: 'core' | 'lan' | 's1' | 's1-socks' | null;
  core: boolean;
  online: boolean;
  last_ts: number | null;
  version: string | null;
}

/** `GET /machines`: машина ядра первой; версия ядра — `items[0].version`. */
export interface MachineList {
  items: Machine[];
  core: string | null;
}

/** Ф82-1: набор «Моих данных» — `common` или проект (`scope` — id строкой). */
export interface MyDataSet {
  scope: string;
  project_id: number | null;
  title: string;
  /** Ключей в файле набора (у проекта — и «явно пустые»). */
  count: number;
  broken: boolean;
}

export interface MyDataCatalogKey {
  key: string;
  label: string;
}

/** `GET /my-data`. */
export interface MyDataOverview {
  sets: MyDataSet[];
  catalog: MyDataCatalogKey[];
  value_max: number;
}

/** Поле набора БЕЗ значения (`GET /my-data/{scope}`). `source` — откуда действует значение; `null` — нигде. */
export interface MyDataField {
  key: string;
  label: string;
  source: 'common' | 'project' | null;
  filled: boolean;
  set: boolean;
  length: number;
  by: 'owner_channel' | 'agent' | null;
  ts: number | null;
  /** Только у проекта: значение берётся из общего набора. */
  from_common?: boolean;
}

export interface MyDataFields {
  scope: string;
  fields: MyDataField[];
}

/** Ф41-2а: состояние сессии хаба (запись ядра): процесс есть и идёт ход / ждёт входа / процесса нет. */
export type HeldState = 'running' | 'idle' | 'asleep';

/** Строка `items` из `GET /subs`. `project_id` 0 — агент вне проектов (тогда `cwd` — папка сессии).
 * `role`, `session_name`, `cwd` — чужой ввод: только текстом. */
export interface ProjectSub {
  id: number;
  project_id: number;
  role: string | null;
  role_no: number | null;
  mode: SubMode;
  msg_filter?: string | null;
  /** Ф38-1: фильтр по аккаунтам бесед — «только эти» или «все, кроме этих»; `null` — все аккаунты. */
  accounts?: { only?: number[]; except?: number[] } | null;
  /** Ф67-1: действующий интервал пачки сигналов подписки, минуты (0 — сразу; ядро старше — поля нет). */
  batch_min?: number;
  engine: EngineName;
  session_name: string | null;
  live: boolean;
  listening: boolean;
  waiting: boolean;
  pending?: number;
  created_ts?: number;
  cwd?: string | null;
  /** Ф41-1: только у сессий Claude Code; `null` — стенограмма не найдена. */
  cache?: SessionCache | null;
  /** Ф41-2а: подписка сессии хаба — жива, пока запись не закрыта (и когда процесса нет). */
  held?: boolean;
  hub_session_id?: number;
  state?: HeldState;
  /** Ф77-1: машина подписки; старое ядро поля не шлёт. */
  machine?: Machine | null;
}

/** Одна сторона сообщения агентов. */
export interface PeerSide {
  project_id: number;
  role: string | null;
  role_no: number | null;
  sub_id: number | null;
  engine?: EngineName;
  /** Ф77-1: машина отправителя/адресата. */
  machine?: Machine | null;
}

export type PeerMsgStatus = 'pending' | 'read' | 'lost' | 'expired';

/** Сообщение агента агенту или диалог владелец <-> агент. `from === null` — писал владелец, `to === null` — адресат владелец.
 * `text` и `reason` — чужой ввод: только текстом. */
export interface PeerMsg {
  id: number;
  from_user: boolean;
  to_user: boolean;
  from: PeerSide | null;
  to: PeerSide | null;
  text: string;
  reply_to: number | null;
  chain_id: number | null;
  hop: number;
  status: PeerMsgStatus;
  reason: string | null;
  created_ts: number;
  signaled_ts: number | null;
  signals: number;
  read_ts: number | null;
  closed_ts: number | null;
  expires_ts: number | null;
  seq: number;
  /** Ф48-4: срочное сообщение агента (мимо пачки адресата). Необязательное — старое ядро. */
  urgent?: boolean;
  /** Ф48-4: срок пачки, которого ждёт сигнал об этом сообщении (мс); `null` — пачки нет. Необязательное. */
  batch_ts?: number | null;
}

export interface PeerSendResult {
  msg: PeerMsg;
  recipient: { live: boolean; waiting: boolean };
}

// --- Ф41-2б-2: постоянные сессии хаба (`core/routes/sessions.py`, контракт — спека `phase41-02b-held-host-spec.md`, 9) ---

export type HubSessionWait = 'slot' | 'elsewhere' | 'paused' | 'heavy' | 'compact';

/** Публичная запись сессии. `engine_session_id`, `workdir`, `cache` — только куке UI; в элементе `/sync` нет
 * `listening`, `workdir`, `cache` (их обновляет опрос `GET /sessions`). `title`, `role`, `failure` — чужой ввод. */
/** Ф50-4: состояние сессии хаба для оркестратора (`GET /sessions`; в `/sync` нет). */
export interface AgentState {
  /** Ф77-1. */
  machine?: Machine | null;
  held: boolean;
  /** Ф64-1: время закрытия дня проекта, мс; `null` — день открыт. */
  day_closed_ts?: number | null;
  hub_session_id: number;
  state: 'running' | 'idle' | 'asleep';
  turn: { since_ts: number; origin: string } | null;
  sleep: { reason: string; text: string } | null;
  step: { tool: string; ts: number } | null;
  cards: {
    total: number;
    user: number;
  };
  signals: { waiting: number; ready: number; urgent: number };
  mid_queued_ts: number | null;
  bg: { running: number; total: number };
  last_turn_ts: number | null;
  last_error: { ts: number; kind: string; summary: string } | null;
  lines: string[];
}

export interface HubSession {
  id: number;
  project_id: number;
  role: string | null;
  role_no: number | null;
  engine: EngineName;
  model: string | null;
  effort: string | null;
  mode: 'manual' | 'auto';
  title: string | null;
  state: HeldState;
  host_seen_ts: number | null;
  created_ts: number;
  updated_ts: number;
  closed_ts: number | null;
  close_reason: string | null;
  /** `ui` | `request` | `agent` | `adopt` | `handoff` — кто завёл сессию. */
  created_by: string;
  seq: number;
  last_event_id: number | null;
  last_turn_ts: number | null;
  failure: string | null;
  wait_reason: HubSessionWait | null;
  pending_requests: number;
  queued_says: number;
  /** Ф48-2 A: число прерванных реплик (входят в `queued_says`); нет поля — старое ядро. */
  interrupted_says?: number;
  /** Ф48-2 D: свой порог сжатия сессии (50 000…900 000) или `null` — умолчание; выключатель; действующий порог хоста. */
  compact_tokens?: number | null;
  compact_auto?: boolean;
  compact_at?: number | null;
  /** Ф48-3: файл роли — путь к MD-файлу относительно рабочей папки проекта; `null` — не задан. */
  role_file?: string | null;
  /** Ф50-7: подпапка проекта, где идёт процесс; `null` — корень проекта. Чужой ввод — только текстом. */
  subdir?: string | null;
  /** Ф50-7: полный путь папки процесса — только в `GET /sessions/{id}`. */
  cwd?: string;
  /** Ф77-1: машина сессии хаба (машина ядра); нет поля — старое ядро. */
  machine?: Machine | null;
  interrupt_requested: boolean;
  sleep_requested: boolean;
  /** Ф73-1: «Перечитать MCP» нажата и ещё не исполнена (мс нажатия); нет поля или `null` — просьбы нет. */
  mcp_reload_ts?: number | null;
  sub_id: number | null;
  listening?: boolean;
  /** Ф50-4: у кого карточки и на чём стоит сессия; нет поля — `/sync` или старое ядро. */
  agent_state?: AgentState;
  engine_session_id?: string | null;
  workdir?: string;
  cache?: SessionCache | null;
  /** Ф41-3а: контекст и накопленная цена (ядро копит `turn_cost_usd`); `null` — хост ещё не присылал. */
  usage?: SessionUsage | null;
  /** Ф41-4: желаемая подписка (задаёт владелец), живая подписка, расхождение между ними, беседы «без подтверждения» у роли. */
  want_sub?: SessionWantSub | null;
  sub?: SessionSub | null;
  sub_stale?: boolean;
  autosend_channels?: number;
  /** Ф41-5: снятые для сессии выключатели автопилота (у `manual` — пусто); сколько стоит — `auto_held`. */
  auto_free?: string[];
  auto_held?: number;
  /** Ф99-7.2: «Всегда в этой сессии» — ключи, разрешённые без карточки (в любом режиме). */
  always?: SessionAlways[];
  autosend_ok?: boolean;
  /** Ф48-1: фоновые агенты и команды сессии; нет поля или `null` — задач нет (или старое ядро). */
  bg?: SessionBg | null;
  /** Ф83: своя настройка запасного (`null` — как у проекта, `{engine: null}` — нет). */
  fallback?: { engine: EngineName | null; model?: string | null; effort?: string | null } | null;
  /** Ф83: куда перейдёт сессия на паузе (`source` — чья настройка); `null` — запасного нет. */
  fallback_target?: (FallbackChoice & { source: 'session' | 'project' }) | null;
  /** Ф83: «на запасном»; `text` готовит ядро (`on_fallback_text`). */
  on_fallback?: (OnFallback & { by: 'auto' | 'owner_channel' }) | null;
  on_fallback_text?: string | null;
}

/** Ф48-1: одна фоновая задача сессии (контракт — спека `phase48-01-session-bg-tasks-spec.md`, раздел 3). */
export interface SessionBgItem {
  id: string;
  kind: 'agent' | 'command';
  title: string;
  agent_type: string | null;
  parent: string | null;
  /** `running` | `completed` | `failed` | `stopped`, иное — строкой как есть. */
  state: string;
  started_ts: number;
  ended_ts: number | null;
  doing: string | null;
  last_tool: string | null;
  tool_uses: number | null;
  tokens: number | null;
  note: string | null;
}

export interface SessionBg {
  items: SessionBgItem[];
}

/** Ф41-4: желаемый фильтр подписки сессии (поля и пределы — как у тела `POST /subs`; роль и номер — из записи). */
export interface SessionWantSub {
  mode: 'cards' | 'messages' | 'direct';
  msg_filter?: 'all' | 'personal';
  keywords?: string[];
  exclude?: string[];
  batch_min?: number | null;
  accounts?: { only: number[] } | { except: number[] } | null;
  /** Ф77-1: у сессии хаба — всегда машина ядра. */
  machine?: Machine | null;
}

/** Ф41-4: живая подписка сессии на проект; `pending` — ждущие сигналы всех видов. */
export interface SessionSub {
  id: number;
  mode: 'cards' | 'messages' | 'direct';
  msg_filter: 'all' | 'personal';
  keywords: string[];
  exclude: string[];
  batch_min: number | null;
  accounts: { only: number[] } | { except: number[] } | null;
  pending: number;
  /** Ф77-1. */
  machine?: Machine | null;
}

/** Ф41-3а: расход сессии (`main.hub_session.usage_json`); `context_limit` и `cost_usd` бывают `null`. */
export interface SessionUsage {
  /** `null` после сжатия — размер уточнится после следующего хода. */
  context_tokens: number | null;
  context_limit: number | null;
  /** Ф41-4 П17: размер контекста после первого хода разговора (ниже сжатие не опустит); интерфейс не показывает. */
  base_tokens?: number | null;
  cost_usd: number | null;
  updated_ts: number;
}

/** Запрос сессии хаба: `input` — вход инструмента целиком (или `{_truncated: "..."}` при обрезке). */
/** Ф99-7.2: одно «всегда» сессии — `mcp:<инструмент MCP>` или `dir:<каталог записи вне проекта>`. */
export interface SessionAlways {
  key: string;
  ts: number | null;
}

export interface SessionRequest {
  id: number;
  session_id: number;
  project_id: number | null;
  kind: 'permission' | 'question';
  tool: string | null;
  input: unknown;
  input_truncated: boolean;
  reason: string | null;
  status: WorkRequestStatus;
  answer: unknown;
  created_ts: number;
  decided_ts: number | null;
  delivered: boolean;
  seq: number;
  /** Ф41-5: ключ выключателя автопилота, который придержал запрос, и его название. */
  rule?: string | null;
  rule_title?: string | null;
  /** Ф49-1: кто решил, если не владелец. */
  decided_by?: { role: string; role_no?: number; sub?: number; auto?: boolean } | null;
  /** Ф99-7.2: ключ «Всегда в этой сессии» этой карточки (`mcp:<инструмент>` | `dir:<каталог>`); `null` — кнопки нет. */
  always?: string | null;
}

/** Ф99-7.3: ответ `GET /subs/{id}/transcript`. */
export interface SubTranscript {
  supported: boolean;
  engine: string;
  remote: boolean;
  /** Раз в сколько секунд обновляется (сессия другой машины); у сессии на машине ядра — `null`. */
  refresh_s: number | null;
  events: SessionEvent[];
  updated_ms: number | null;
}

export type SessionEventKind = WorkEventKind | 'interrupt' | 'compact';

/** Ф41-3а: короткое описание шага для строки ленты (`brief` события, до 1000 байт). */
export type SessionBrief =
  | { type: 'diff'; file: string; added: number; removed: number; cut?: boolean }
  | { type: 'output'; lines: number; cut?: boolean }
  | { type: 'signal'; cards?: number; messages?: number; reminders?: number; agents?: number; from_user?: number; drafts?: number; presses?: number; channels?: number; cut?: boolean }
  | { type: 'compact'; before?: number | null; after?: number | null; cut?: boolean }
  | { type: 'files'; files: { name: string; size: number; kind: string }[]; cut?: boolean }
  | {
      type: 'usage';
      duration_ms: number;
      steps: number;
      cost_usd: number | null;
      tokens: { input: number; output: number; cache_read: number; cache_write: number } | null;
      cut?: boolean;
    }
  | { type: string; cut?: boolean; [key: string]: unknown };

/** Ф41-3а: подробности шага по клику (`GET /sessions/{id}/events/{event_id}`, поле `detail`). */
export type SessionEventDetail =
  | { type: 'input'; command?: string; description?: string; input?: unknown; _cut?: boolean }
  | { type: 'diff'; file: string; patch: string; cut?: boolean }
  | { type: 'output'; text: string; cut?: boolean }
  | { type: string; [key: string]: unknown };

export interface SessionEvent extends Omit<WorkEvent, 'kind'> {
  kind: SessionEventKind;
  /** Ф41-3а: нет у старого ядра и у событий без подробностей. */
  brief?: SessionBrief | null;
  has_detail?: boolean;
}

/** Ф41-3а: событие целиком (ручка одного события). */
export interface SessionEventFull extends SessionEvent {
  detail: SessionEventDetail | null;
}

/** Ф41-3в: ответ `POST /sessions/{id}/files`. */
export interface SessionFileResult {
  id: string;
  name: string;
  size: number;
  mime: string;
  kind: 'image' | 'pdf' | 'file';
}

/** Ф41-3в: приложенное к реплике — чип над кнопками окна (черновик живёт только в сторе страницы). */
export interface SessionFileDraft {
  localId: number;
  name: string;
  size: number;
  kind: SessionFileResult['kind'] | null;
  status: 'uploading' | 'ready' | 'error';
  fileId: string | null;
  error: string | null;
}

/** Ф60-1C: запись интервью (`interview` в ответах ручек `/interviews` и кадрах `event: interview`); время — мс от эпохи. */
export interface Interview {
  id: number;
  session_id: number;
  project_id: number;
  mode: 'live' | 'button';
  them_lang: 'ru-RU' | 'en-US';
  status: 'active' | 'paused' | 'ended';
  started_ts: number;
  ended_ts: number | null;
  fed_line_id: number;
  hint_ts: number | null;
  reason: string | null;
  warn: string | null;
  last_line_ts: number | null;
  lines: number;
  queued_says: number;
  wait_reason: string | null;
}

/** Строка расшифровки. `text` — чужая речь: только текстовая интерполяция. */
export interface InterviewLine {
  id: number;
  interview_id: number;
  who: 'me' | 'them';
  start_ms: number;
  end_ms: number;
  text: string;
  created_ts: number;
}

/** `GET /interviews/current`. */
export interface InterviewCurrent {
  interview: Interview | null;
  lines: InterviewLine[];
  listener_seen_ms: number | null;
  listener_alive: boolean;
}

/** `POST /interviews/{id}/hint` и `/end`. */
export interface InterviewHintResult {
  interview: Interview;
  queued: boolean;
  says: number[];
  lines: number;
}

/** Кадр `event: interview` потока `/sessions/live`. */
export type InterviewFrame =
  | { type: 'line'; interview_id: number; session_id: number; line: InterviewLine }
  | {
      type: 'state';
      interview_id: number;
      session_id: number;
      interview: Interview;
      listener_seen_ms: number | null;
      listener_alive: boolean;
    }
  | { type: 'resync' };

/** Ф41-3б: набираемый сейчас текст сессии (кадр `live` потока `/sessions/live`). */
export interface SessionLive {
  block: string;
  kind: 'text' | 'thinking';
  text: string;
  cut: boolean;
  done: boolean;
}

/** Реплика в очереди (`says` в `GET /sessions/{id}`). */
export interface SessionSay {
  id: number;
  session_id: number;
  text: string;
  priority: 'next' | 'now';
  created_ts: number;
  /** Ф48-2 A: `interrupted` — ход оборван кнопкой «Прервать», реплика ждёт решения владельца. Нет поля — `queued`. */
  status?: 'queued' | 'interrupted';
  interrupted_ts?: number | null;
  resent_ts?: number | null;
}

export interface SessionDetail {
  session: HubSession;
  requests: SessionRequest[];
  says: SessionSay[];
}

export interface SessionCreateInput {
  project_id: number;
  engine: EngineName;
  role?: string;
  number?: number;
  model?: string;
  effort?: string;
  title?: string;
  /** Ф41-5: режим сессии; без поля — ручной. */
  mode?: 'manual' | 'auto';
  /** Ф41-4: желаемая подписка; без поля — «не подписывать». */
  want_sub?: SessionWantSub | null;
  /** Ф48-3: файл роли (путь в проекте); без поля — нет. */
  role_file?: string;
  /** Ф50-7: подпапка проекта; без поля — корень. После создания не меняется. */
  subdir?: string;
  /** Ф99-1: id прошлого разговора Claude Code папки проекта — сессия продолжает его; без поля — новый разговор.
   * Только движок Claude Code и без подпапки. */
  resume_conv?: string;
}

/** Ф99-1: прошлый разговор Claude Code папки проекта (`GET /projects/{id}/conversations`). */
export interface ProjectConversation {
  id: string;
  /** Последняя запись, мс. */
  last_ts: number;
  /** Размер стенограммы, байт. */
  size: number;
  /** Начало первой реплики человека (маскировано ядром); пусто — реплик нет. */
  preview: string;
  /** Номер незакрытой сессии хаба, которая ведёт этот разговор; `null` — свободен. */
  hub_session_id: number | null;
}

// Ф56-3: единая очередь владельца (`GET /owner_channel/queue`, контракт — план фазы 56, раздел 10). Тексты `brief`/`full`
// маскирует ядро; интерфейс рисует их только как текст.
export interface OwnerAnswer {
  action: string;
  label: string;
  needs_text: boolean;
  takes_text?: boolean;
  needs_answers?: boolean;
}

export interface OwnerAgentAction {
  id: string;
  label: string;
  kinds?: string[];
  needs_text: boolean;
}

export interface OwnerErrand {
  id: number;
  action: string | null;
  label: string | null;
  text: string | null;
  created_ts: number;
  progress: string | null;
  /** Ф65-1: время последнего шага; агент взял сам (`auto`). Старое ядро полей не шлёт. */
  progress_ts?: number | null;
  auto?: boolean;
  /** Ф96-1: у кого руки — `agent` (по умолчанию, ключа нет) | `owner_channel`. */
  hands?: 'agent' | 'owner_channel';
  /** Кто передал последним: `ui` | `bot` | `agent` | null. */
  hands_by?: string | null;
  /** Мс последней передачи рук (null — не передавались). */
  hands_ts?: number | null;
  /** Что написал агент, передавая руки (маска). */
  hands_why?: string | null;
}

/** Ф58-3: агент вернул поручение (`POST /owner_channel/errands/{id}/return`) — причина и время возврата.
 *  Ф65-1: `by` — кто вернул (`agent` / `system` — завис / `ui` — остановил или ответил владелец), `detail` — подробности. */
export interface OwnerErrandReturned {
  reason: string;
  ts: number;
  by?: 'agent' | 'system' | 'ui';
  detail?: string | null;
}

/** Ф65-1: открытый вопрос агента владельцу по карточке (ответ — `POST /owner_channel/talk {text, reply_to: id}`). */
export interface OwnerAgentAsk {
  id: number;
  text: string;
  ts: number;
  until_ts: number | null;
}

/** Ф65-1: строка `GET /owner_channel/closed` (нужны только поля итога). */
export interface OwnerClosedRow {
  id: number;
  ref: string;
  kind: string;
  project_id: number | null;
  closed_ts: number;
  closed_by: string | null;
  /** Ф66-1: `{ by: 'auto', … }` — закрыто «сразу да»; иное содержимое не используется. */
  payload?: { by?: string } | null;
  errand_result: string | null;
  /** Ф92-1 (A2): карточка `turn`, закрытая агентом, — что он сделал и что ответил сессии (маскированный текст). */
  agent_reply?: { action: 'reply' | 'ok' | 'deny'; text: string | null } | null;
  /** Ф76-3: основание решения агента (`rule:…` | `pattern:…` | `similar:…` | `card` | `none`). */
  basis?: string | null;
}

/** Ф76-4б: образец решений владельца (`GET /patterns/{project_id}` → `patterns`). */
export interface DecisionPattern {
  key: string;
  line: string;
  verdict?: string | null;
  count: number;
  last_ts: number | null;
  kept?: boolean;
  status: 'active' | 'young' | 'faded' | 'dropped';
  always_me?: boolean;
  /** Форма примеров ядро не фиксирует: строка или объект с текстами карточки и ответа. */
  examples?: Array<string | { ts?: number; ask?: string; answer?: string; text?: string }>;
}

export interface PatternsState {
  project_id: number;
  rules: string | null;
  keep: string[];
  drop: string[];
  always_me: string[];
  patterns: DecisionPattern[];
  min_count: number;
  window_days: number;
}

export interface PatternsPut {
  rules?: string | null;
  keep?: string[];
  drop?: string[];
  always_me?: string[];
}

/** Ф76-4б: строка `GET /patterns/{project_id}/agent-decisions`. */
export interface AgentDecision {
  id: number;
  ts: number;
  ref: string;
  project_id: number | null;
  kind: string;
  role: string | null;
  action: string | null;
  verdict: string | null;
  text: string | null;
  basis: string | null;
  pattern_key: string | null;
  wrong_ts: number | null;
}

/** Модель проекта (`GET /owner_channel/agent-settings/projects/{id}`): `model = null` — как общая. */
export interface OwnerProjectModel {
  model: string | null;
  variant: string | null;
}

export interface OwnerPath {
  project: string | null;
  head: string | null;
  author: string | null;
  about?: string | null;
}

export interface OwnerItem {
  ref: string;
  kind: string;
  project_id: number | null;
  project_name: string | null;
  session_id: number | null;
  role: string | null;
  role_no: number | null;
  path: OwnerPath;
  brief: string;
  full: Record<string, unknown>;
  created_ts: number;
  until_ts: number | null;
  in_channel: boolean;
  hold_ts: number | null;
  answers: OwnerAnswer[];
  agent_actions: OwnerAgentAction[];
  errand: OwnerErrand | null;
  /** Ф58-3: последнее поручение по карточке агент вернул; старое ядро поля не шлёт. */
  errand_returned?: OwnerErrandReturned | null;
  /** Ф65-1: агент берёт карточку сам (маршрут группы — `agent`). */
  auto_ok?: boolean;
  /** Ф65-1: открытый вопрос агента владельцу по этой карточке. */
  agent_ask?: OwnerAgentAsk | null;
  /** Ф77-1: `display` машины сессии карточки; сессии нет — `null`; старое ядро поля не шлёт. */
  machine_name?: string | null;
  /** Ф93-1: денежная карточка (строка «Деньги» таблицы маршрута); старое ядро поля не шлёт. */
  money?: boolean;
}

export interface OwnerQueue {
  items: OwnerItem[];
  seq: number;
  /** Ф58-1: `talk_unread` — непрочитанные владельцем реплики агента, `talk_open` — реплики владельца без обработки; старое ядро их не шлёт. */
  agent: { online: boolean; seen_ts: number | null; talk_unread?: number; talk_open?: number; talk_progress?: number };
}

/** Ф58-1: реплика разговора владельца с его агентом (`/owner_channel/talk`). */
export interface OwnerTalkMsg {
  id: number;
  ts: number;
  author: 'owner_channel' | 'agent';
  text: string;
  ref: string | null;
  reply_to: number | null;
  handled_ts: number | null;
  read_ts: number | null;
  // Ф61-1: реплика агента — вопрос владельцу (бот перешлёт его в Telegram); у реплики владельца `via = 'bot'` — ответ из
  // Telegram. Необязательные: ядро до Ф61-1 их не отдаёт.
  ask?: boolean;
  ask_until_ts?: number | null;
  closed_ts?: number | null;
  closed_outcome?: 'answered' | 'gave_up' | null;
  via?: 'bot' | null;
  // Ф66-2: ветка реплики — id сессии хаба, `null` — «Общее». Старое ядро поля не шлёт.
  session_id?: number | null;
  // 23.10: строка хода агента у реплики владельца (план, шаги, время), пока реплика не обработана.
  progress?: string | null;
  progress_ts?: number | null;
  // 23.10: файлы реплики владельца; `gone` — байты уже стёрты (имя помнится). Старое ядро поля не шлёт.
  files?: OwnerTalkFile[];
}

/** 23.10: файл реплики разговора с агентом. */
export interface OwnerTalkFile extends SessionFileResult {
  gone: boolean;
}

/** Ф66-2: ветка разговора с агентом (`GET /owner_channel/talk/branches`); `session_id = null` — «Общее». */
export interface OwnerTalkBranch {
  session_id: number | null;
  title: string;
  project_id: number | null;
  role: string | null;
  last_id: number;
  last_ts: number;
  unread: number;
}

/** Ф75-1: модель агента владельца из OpenCode службы (`PUT /owner_channel/agent-models` присылает служба при старте). */
export interface OwnerAgentModel {
  id: string;
  name: string | null;
  images: boolean | null;
  variants: string[];
  efforts?: string[];
}

/** Ф75-1: настройки модели агента владельца (`GET /owner_channel/agent-settings`); `model = null` — выбора нет, действует
 * `default` (роль `agent` из `engine.json` или DeepSeek Flash · max). Пишет только кука (`PUT`). */
export interface OwnerAgentSettings {
  /** Ф78-2: движок, чьи модели в списке; старое ядро не присылает — это OpenCode. */
  engine?: EngineName;
  model: string | null;
  variant: string | null;
  set_ts: number | null;
  default: { model: string; variant: string | null };
  models: OwnerAgentModel[];
  models_ts: number | null;
}

export interface OwnerAnswerBody {
  action: string;
  text?: string;
  answers?: Record<string, string>;
  send_now?: boolean;
  /** id файлов, загруженных в сессию карточки (только реплика сессии). */
  files?: string[];
}

// --- Ф56-5б: терминал (контракт 5а, `docs/architecture.md`) -------------------------------

export interface TermInfo {
  id: number;
  title: string;
  cols: number;
  rows: number;
  created_ts: number;
  alive: boolean;
  command: string;
  owner?: 'ui' | 'agent';
}

export interface TermRunRecord {
  id: number;
  ts: number;
  term_id: number;
  who: string;
  source_kind: string;
  source_ref: string | null;
  text: string;
}

export interface TermRunSource {
  kind: 'ui_block' | 'agent';
  ref?: string;
}

export interface TermModes {
  cursor_flag?: boolean;
  keypad_cursor_flag?: boolean;
  keypad_flag?: boolean;
  mouse_any_flag?: boolean;
  mouse_button_flag?: boolean;
  mouse_standard_flag?: boolean;
  mouse_sgr_flag?: boolean;
  mouse_utf8_flag?: boolean;
  insert_flag?: boolean;
  wrap_flag?: boolean;
  origin_flag?: boolean;
  scroll_region?: [number, number];
}

/** Кадр `snapshot` потока `GET /term/stream`. */
export interface TermSnapshotFrame {
  term_id: number;
  pos: string;
  cols: number;
  rows: number;
  data_b64: string;
  cursor: [number, number];
  alt_screen: boolean;
  modes: TermModes;
}

export interface TermOutFrame {
  term_id: number;
  pos: string;
  data_b64: string;
}

/** Ф63-1 (`docs/architecture.md`): правило входящих проекта. */
export interface InboundRule {
  id: number;
  project_id: number;
  title: string;
  enabled: boolean;
  match: { accounts: number[]; from: string[]; subject: string[]; text: string[]; exclude: string[]; kinds: string[] };
  action: { wake: string[]; attention: 'react' | 'info' | null; archive?: boolean };
  description: string;
  created_by_role: string | null;
  created_by_session: number | null;
  created_ts: number;
  updated_ts: number;
  hits: number;
  last_hit_ts: number | null;
  wake_status: { role: string; listening: 'messages' | 'cards' | 'none' }[];
}

export interface InboundRulesList {
  project_id: number;
  rules: InboundRule[];
  limits: { rules: number; list: number; title: number; words: number };
}

export interface InboundRuleInput {
  title: string;
  accounts?: number[];
  from?: string[];
  subject?: string[];
  text?: string[];
  exclude?: string[];
  kinds?: string[];
  wake?: string[];
  attention?: 'react' | 'info' | null;
  /** Беседа сразу в архив (владелец 22.10). */
  archive?: boolean | null;
  enabled?: boolean;
}

/** Ф72-1: `GET /files/md` — MD-файл для просмотра в хабе. `path` — настоящий абсолютный путь найденного файла. */
export interface MdFile {
  path: string;
  text: string;
  size: number;
  mtime_ms: number;
}
