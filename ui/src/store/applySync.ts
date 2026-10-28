// Чистый редьюсер состояния ленты. НЕ импортирует vue (спека: «src/store/ … чистый редьюсер applySync
// отдельным модулем без импорта Vue») — тестируется на голых объектах, без DOM и без реактивности; вызывающая
// сторона (`store.ts`) передаёт сюда `reactive(...)`-объект, и редьюсер, ничего не зная про Vue, мутирует
// его поля обычными присваиваниями — реактивность срабатывает у вызывающей стороны бесплатно.
import type { Channel, MediaItem, Message, MessageOp } from '../api/types';
import {
  compareMessages,
  createThreadsState,
  isReply,
  updateThreadParentIfLoaded,
  upsertThreadReply,
  type ThreadsMap,
} from './threads';

// РАСХОЖДЕНИЕ (см. отчёт исполнителя 6b): порядок показа по спеке — `(ts, ext_sort, id)`
// (`docs/architecture.md`; `queries.py`, `_MESSAGE_LIST_TEMPLATE`), но `ext_sort` — служебная
// колонка СУБД, которая в JSON сообщения клиенту НЕ отдаётся (`queries._message_row_to_dict` её не включает).
// Чтобы страница `/messages` и строки `/sync` сортировались в браузере ТАК ЖЕ, как отдаёт SQL, редьюсер
// воспроизводит `_ext_sort()` (`src/hub/core/ingest.py`/`outbox.py`) из уже видимого поля `ext_id`: числовой
// ext_id (только ASCII-цифры, ≤18 знаков) — числом, иначе (в т.ч. `null` у заготовки outbox) — 0. Эквивалентно
// серверному правилу байт-в-байт; единственное отличие — источник данных (вычисляется, а не читается).
// Сама функция `compareMessages` (спека 2e перенесла её в `store/threads.ts`, чтобы ею мог пользоваться и
// тред без циклического импорта) — реэкспортирована здесь под тем же именем ради `visibleMessage.ts`.
export { compareMessages };

export interface SyncState {
  channelsById: Record<number, Channel>;
  messagesByChannel: Record<number, Message[]>;
  // Спека 2e, раздел «Стор»: сводка + ответы открытых/загруженных тредов Slack, по ключу
  // `"<channelId>:<threadExtId>"` (`store/threads.ts::threadKey`).
  threads: ThreadsMap;
  lastSeq: number;
}

export function createSyncState(): SyncState {
  return { channelsById: {}, messagesByChannel: {}, threads: createThreadsState(), lastSeq: 0 };
}

export interface SyncBatch {
  messages: Message[];
  channels: Channel[];
  max_seq: number;
}

function upsertMessage(state: SyncState, message: Message): void {
  let list = state.messagesByChannel[message.channel_id];
  if (list === undefined) {
    list = [];
    state.messagesByChannel[message.channel_id] = list;
  }
  const idx = list.findIndex((m) => m.id === message.id);
  if (idx === -1) {
    list.push(message);
  } else {
    list[idx] = message;
  }
  list.sort(compareMessages);
}

/** Применить пачку изменений (`/sync` или итог одного кадра SSE + догоняющий `/sync`). Идемпотентна:
 * повторное применение той же пачки не меняет состав/порядок (upsert по `id`, `lastSeq` только растёт).
 *
 * Спека 2e, раздел «Стор» (закрывает временное правило куска 2b — «ответ не кладётся в ленту канала»):
 * ответ (`isReply`) идёт в `state.threads[key].replies`, ТОЛЬКО если тред уже загружен — в ленту канала НЕ
 * попадает, кроме `thread_broadcast` (Slack показывает такой ответ и в канале — раздел «Модель тредов» 2b).
 * Родитель (не ответ) — как раньше, в ленту, плюс обновляет `threads[key].parent`, если его тред загружен. */
export function applySync(state: SyncState, batch: SyncBatch): void {
  for (const channel of batch.channels) {
    // Ф8-4 B4: беседа удалена владельцем в хабе — из списка и из памяти ленты прочь (вернётся с новым сообщением).
    if (channel.hidden) {
      removeChannel(state, channel.id);
      continue;
    }
    // Спека 1d-1, п.5: `parent_title` есть только в ответе `GET /projects/{id}/channels`, изменения из
    // `/sync`/`/stream` этого поля не несут вовсе (`undefined`, не `null` — см. api/types.ts). Без этой
    // защиты первое же изменение канала (unread, muted, правка заголовка…) стирало бы подпись форума у
    // топика, уже открытого в списке. `null` (осознанно «родителя нет») из самого `/sync` не пришёл бы —
    // сюда попадает только явный `undefined`.
    const previous = state.channelsById[channel.id];
    state.channelsById[channel.id] =
      channel.parent_title === undefined && previous?.parent_title !== undefined
        ? { ...channel, parent_title: previous.parent_title }
        : channel;
  }
  for (const message of batch.messages) {
    // Ф8-4 B4: заглушка скрытого (`{id, channel_id, hidden}`) — убрать строку из ленты и тредов.
    if (message.hidden) {
      removeMessage(state, message.id);
      continue;
    }
    if (isReply(message)) {
      upsertThreadReply(state.threads, message);
      if (message.thread_broadcast) upsertMessage(state, message);
      continue;
    }
    upsertMessage(state, message);
    updateThreadParentIfLoaded(state.threads, message);
  }
  if (batch.max_seq > state.lastSeq) {
    state.lastSeq = batch.max_seq;
  }
}

/** Загрузка страницы `GET /channels/{id}/messages` (не изменение по `seq`, но та же дедуп-логика по `id`
 * пригождается: повторная загрузка той же страницы, например после реконнекта, не даёт дублей). */
export function mergeMessagePage(state: SyncState, messages: Message[]): void {
  for (const message of messages) {
    upsertMessage(state, message);
  }
}

/** Ф8-5 П8: ответ `fetch` несёт обновлённый `media` сообщения — положить его сразу во все места, где это
 * сообщение уже лежит (лента канала, ответы и родитель загруженных тредов), не дожидаясь `/sync`. */
export function applyMediaUpdate(state: SyncState, messageId: number, media: MediaItem[]): void {
  const lists: Message[][] = [...Object.values(state.messagesByChannel)];
  for (const thread of Object.values(state.threads)) {
    lists.push(thread.replies);
    if (thread.parent !== null) lists.push([thread.parent]);
  }
  for (const list of lists) {
    for (const message of list) {
      if (message.id === messageId) message.media = media;
    }
  }
}

/** Ф14-8 раздел D: `op` из ответа ручки правки/удаления/отмены — сразу во все копии сообщения (лента, ответы и
 * родители тредов), не дожидаясь `/sync`; дальше правда приходит обычным `/sync`. */
export function applyMessageOp(state: SyncState, messageId: number, op: MessageOp | null): void {
  const lists: Message[][] = [...Object.values(state.messagesByChannel)];
  for (const thread of Object.values(state.threads)) {
    lists.push(thread.replies);
    if (thread.parent !== null) lists.push([thread.parent]);
  }
  for (const list of lists) {
    for (const message of list) {
      if (message.id === messageId) message.op = op;
    }
  }
}

/** Ф8-4 П6: родитель треда, удалённый у себя в хабе, — заглушка без содержимого («удалено у себя»). */
export function hiddenStub(message: Message): Message {
  return {
    ...message,
    hidden: true,
    text: null,
    entities_json: null,
    attachments_json: null,
    forward_json: null,
    mail_to_json: null,
    refs_json: null,
    reactions_json: null,
    buttons: null,
    sender: null,
    media: [],
  };
}

/** Ф8-4 B4: убрать сообщение отовсюду в сторе — лента канала, ответы загруженных тредов; родитель загруженного
 * треда остаётся заглушкой (П6: новые ответы треда видны и после удаления родителя). */
export function removeMessage(state: SyncState, messageId: number): void {
  for (const list of Object.values(state.messagesByChannel)) {
    const idx = list.findIndex((m) => m.id === messageId);
    if (idx !== -1) list.splice(idx, 1);
  }
  for (const thread of Object.values(state.threads)) {
    const idx = thread.replies.findIndex((m) => m.id === messageId);
    if (idx !== -1) thread.replies.splice(idx, 1);
    if (thread.parent !== null && thread.parent.id === messageId && !thread.parent.hidden) {
      thread.parent = hiddenStub(thread.parent);
    }
  }
}

/** Ф8-4 B4: беседа удалена у себя в хабе — канал, его лента и загруженные треды прочь из стора. */
export function removeChannel(state: SyncState, channelId: number): void {
  delete state.channelsById[channelId];
  delete state.messagesByChannel[channelId];
  const prefix = `${channelId}:`;
  for (const key of Object.keys(state.threads)) {
    if (key.startsWith(prefix)) delete state.threads[key];
  }
}

export function setChannels(state: SyncState, channels: Channel[]): void {
  for (const channel of channels) {
    // Ф99-2: список бесед проекта не несёт `muted_by_*` (их отдают `/sync` и `GET /channels/{id}`) — не стирать.
    const previous = state.channelsById[channel.id];
    state.channelsById[channel.id] =
      channel.muted_by_role === undefined && channel.muted && previous?.muted_by_role
        ? { ...channel, muted_by_role: previous.muted_by_role, muted_by_note: previous.muted_by_note }
        : channel;
  }
}

export function sortedMessagesOf(state: SyncState, channelId: number): Message[] {
  return state.messagesByChannel[channelId] ?? [];
}
