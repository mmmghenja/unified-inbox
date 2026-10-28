<script setup lang="ts">
import { computed, inject, nextTick, ref, watch } from 'vue';

import type { Message, MessageEdit } from '../api/types';
import { parseAttachments } from '../render/attachments';
import { parseForward } from '../render/forward';
import { parseMailTo } from '../render/mailTo';
import {
  QUICK_REACTIONS,
  channelReactions,
  isNamedReaction,
  isReactionSource,
  moreReactionsFor,
  parseReactions,
} from '../render/reactions';
import { store } from '../store/instance';
import { openPersonOfContact, senderClickable } from '../store/people';
import {
  displayReactions,
  pendingOps,
  reactionError,
  settleReactions,
  toggleReaction,
} from '../store/reactions';
import { opensThreadOnReply, replyTargetThreadExtId } from '../store/threads';
import { IN_BOARD_COLUMN } from '../util/boardCtx';
import { confirmAndHideMessages } from '../util/hide';
import {
  ENTITIES_LOST_WARNING,
  REVOKE_CONFIRM,
  canEdit,
  canRevoke,
  editLosesEntities,
  opFailureReason,
  opRequestErrorText,
} from '../util/msgOps';
import { peerReadMark, peerReadTitle } from '../util/peerRead';
import { usePopup } from '../util/popup';
import { replyPreviewLabel } from '../util/replyPreview';
import { onSubmitKey } from '../util/submitKeys';
import { pressButton } from '../store/buttons';
import { buttonState } from '../store/pressView';
import AttachmentList from './AttachmentList.vue';
import DictateButton from './DictateButton.vue';
import MessageButtons from './MessageButtons.vue';
import { showMentions } from '../util/mentions';
import MessageText from './MessageText';
import { isMarkdownSource } from '../util/textFormat';
import OutboxStatusBadge from './OutboxStatusBadge.vue';
import PostViews from './PostViews.vue';
import ReactionsLine from './ReactionsLine.vue';

const props = defineProps<{ message: Message }>();
const emit = defineEmits<{ reply: [message: Message] }>();

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Ф8-1 C2: удалённое отправителем показывается как
// обычно — текст, вложения, пересылка, цитата ответа, реакции — но приглушённо и с пометкой «удалено» у
// времени. Ядро текст больше не прячет (`queries._message_row_to_dict`, C1). Удалённое без текста, но с
// вложениями или пересылкой (фото без подписи) — тоже целиком (решение архитектора 24.09). «Сообщение
// удалено» вместо содержимого — только когда показать нечего: ни текста, ни вложений, ни пересылки.
const postShown = ref(false);
const forward = computed(() => parseForward(props.message.forward_json));
// Ф11-8: адресаты своего письма — «кому: …» (только текстом, полный список в `title`).
const mailTo = computed(() => parseMailTo(props.message.mail_to_json));
const attachments = computed(() => parseAttachments(props.message.attachments_json));
const deletedNothingToShow = computed(
  () =>
    props.message.deleted &&
    props.message.text === null &&
    attachments.value.length === 0 &&
    forward.value === null,
);
// --- Ф10-3 (раздел D, П1–П3, П10): реакции владельца — только Telegram/Slack, сообщение источника (`ext_id`), не
// удалённое, не скрытое, не служебное, не своя заготовка. Альбомы (`AlbumRow`) — вне куска. -----------------
const reactionSource = computed(() => {
  const source = store.state.sync.channelsById[props.message.channel_id]?.source;
  return isReactionSource(source) ? source : null;
});
// Ф15-1 D: письмо — только с `can_react` от ядра (адресаты на Gmail, ящик с отправкой).
const canReact = computed(
  () =>
    reactionSource.value !== null &&
    (reactionSource.value !== 'mail' || props.message.can_react === true) &&
    props.message.ext_id !== null &&
    !props.message.deleted &&
    !props.message.hidden &&
    props.message.kind !== 'event',
);
const reactButtonTitle = computed(() =>
  reactionSource.value === 'mail' ? 'Реакция — в письме реакцию после отправки не снять' : 'Реакция',
);
const reactions = computed(() => {
  if (deletedNothingToShow.value) return [];
  const parsed = parseReactions(props.message.reactions_json);
  if (!canReact.value) return parsed;
  return displayReactions(reactionSource.value, parsed, pendingOps(props.message.id));
});
const reactionErrorText = computed(() => reactionError(props.message.id));
const quickReactions = computed(() => (reactionSource.value === null ? [] : QUICK_REACTIONS[reactionSource.value]));

// П3: операция в пути снимается, когда мои реакции в `reactions_json` ей соответствуют. `immediate` (ревью):
// правда могла прийти, пока строка была размонтирована (владелец уходил в другую беседу).
watch(
  () => props.message.reactions_json,
  (json) => settleReactions(props.message.id, json),
  { immediate: true },
);

function react(emoji: string): void {
  const source = reactionSource.value;
  if (source === null || !canReact.value) return;
  void toggleReaction(props.message.id, source, props.message.reactions_json, emoji, props.message.channel_id);
}

// Всплывающий ряд быстрых: Esc и клик мимо закрывают.
const pickerOpen = ref(false);
const pickerExpanded = ref(false);
// Ф18-1: «ещё» — сначала ходовые в этой беседе (по загруженной ленте), затем весь набор источника, без
// быстрого ряда. Считается только при открытом ряде.
const moreReactions = computed(() => {
  const source = reactionSource.value;
  if (source === null || !pickerOpen.value) return [];
  const feed = store.state.sync.messagesByChannel[props.message.channel_id] ?? [];
  return moreReactionsFor(source, channelReactions(source, feed.map((m) => m.reactions_json)));
});
const pickerEl = ref<HTMLElement | null>(null);
const reactButtonEl = ref<HTMLElement | null>(null);

// Ф80-1 A2: Esc, нажатие мимо и открытие другого меню закрывают ряд (общий механизм `util/popup`).
usePopup({
  open: pickerOpen,
  button: () => reactButtonEl.value,
  panel: () => pickerEl.value,
  onClose: () => {
    pickerExpanded.value = false;
  },
});

function openPicker(): void {
  pickerOpen.value = true;
}

function closePicker(): void {
  pickerOpen.value = false;
  pickerExpanded.value = false;
}

function togglePicker(): void {
  if (pickerOpen.value) closePicker();
  else openPicker();
}

function onPick(emoji: string): void {
  closePicker();
  react(emoji);
}


// --- Ф11-7: «✓»/«✓✓» у моих отправленных сообщений Telegram (`util/peerRead.ts`). ---------------------------
/** Ф63-1: подсказка к пометке «правило #N» — название из `rules` ленты (в `/sync` его нет — тогда без названия). */
function ruleTitle(id: number): string {
  const title = props.message.rules?.find((r) => r.id === id)?.title;
  return title ? `Сработало правило входящих: ${title}` : 'Сработало правило входящих проекта';
}

const peerRead = computed(() => {
  const channel = store.state.sync.channelsById[props.message.channel_id];
  const mark = peerReadMark(props.message, channel);
  return mark === null ? null : { mark, title: peerReadTitle(mark, channel) };
});

// --- спека 2e, раздел «Семантика «Ответить» по источнику»: кнопка есть у каждого сообщения (не `event`, не
// удалённого, не заготовки без ext_id); Slack-канал -> сразу открыть тред, иначе -> отдать решение наверх
// (плашка ответа в поле ввода ленты — состояние живёт у общего предка ленты и композера). ---------------
const canReply = computed(() => !props.message.deleted && props.message.ext_id !== null);

// Ф46-1: «Написать автору» — группа или тема Telegram, у сообщения есть автор-человек (не я, не бот). Знак id
// автора UI не знает — анонимного админа и канал отсекает ядро (текст отказа показывается ниже).
const canWriteAuthor = computed(() => {
  const channel = store.state.sync.channelsById[props.message.channel_id];
  return (
    channel?.source === 'telegram' &&
    (channel.kind === 'group' || channel.kind === 'topic') &&
    props.message.sender !== null &&
    !props.message.is_me &&
    props.message.is_bot !== true &&
    !props.message.deleted &&
    !props.message.hidden
  );
});
const writeAuthorBusy = ref(false);
const writeAuthorError = ref<string | null>(null);

async function onWriteAuthor(): Promise<void> {
  if (writeAuthorBusy.value) return;
  writeAuthorBusy.value = true;
  writeAuthorError.value = null;
  try {
    await store.writeToAuthor(props.message);
  } catch (err) {
    console.error('write to author failed', err);
    writeAuthorError.value = writeAuthorErrorText(err);
  } finally {
    writeAuthorBusy.value = false;
  }
}

function writeAuthorErrorText(err: unknown): string {
  const message = err instanceof Error && err.message !== '' ? err.message : 'не удалось';
  return `Написать автору не вышло: ${message}`;
}

// Ф41-6б: в столбце беседы на «Главной» тред открывается обычным видом на вкладке проекта.
const inBoardColumn = inject(IN_BOARD_COLUMN, false);

function openThreadHere(channelId: number, threadExtId: string): Promise<void> {
  return inBoardColumn ? store.openThreadInProject(channelId, threadExtId) : store.openThread(channelId, threadExtId);
}

function onReplyClick(): void {
  const channel = store.state.sync.channelsById[props.message.channel_id];
  if (opensThreadOnReply(channel?.source)) {
    const target = replyTargetThreadExtId(props.message);
    if (target !== null) void openThreadHere(props.message.channel_id, target);
    return;
  }
  emit('reply', props.message);
}

// --- раздел «Лента канала»: пометка «из треда» на broadcast-ответе, показанном и в основной ленте. -------
function openBroadcastThread(): void {
  if (props.message.thread_ext_id === null) return;
  void openThreadHere(props.message.channel_id, props.message.thread_ext_id);
}

// --- цитата `reply_to_ext_id` (Telegram): цель ищется среди уже ЗАГРУЖЕННЫХ сообщений канала (без догрузки
// истории — Ф6, раздел «Лента канала»). Клик — прокрутить к цели, если она загружена. -----------------
const replyQuoteTarget = computed<Message | null>(() => {
  const targetExtId = props.message.reply_to_ext_id;
  if (targetExtId === null) return null;
  const list = store.state.sync.messagesByChannel[props.message.channel_id] ?? [];
  return list.find((m) => m.ext_id === targetExtId) ?? null;
});

const replyQuoteLabel = computed<string | null>(() => {
  if (props.message.reply_to_ext_id === null) return null;
  const target = replyQuoteTarget.value;
  return target === null ? 'ответ на сообщение' : replyPreviewLabel(target);
});

function scrollToReplyTarget(): void {
  const target = replyQuoteTarget.value;
  if (target === null) return;
  const el = document.querySelector(`[data-message-id="${target.id}"]`);
  if (el instanceof HTMLElement) el.scrollIntoView({ block: 'center' });
}

// --- сводка треда под родителем (спека 2e, раздел «Лента канала»): N = max(reply_count, source_reply_count
// ?? 0) — источник иногда знает об ответах, которых ядро ещё не догрузило (старый тред Slack). ------------
function pluralReplies(n: number): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return 'ответов';
  if (mod10 === 1) return 'ответ';
  if (mod10 >= 2 && mod10 <= 4) return 'ответа';
  return 'ответов';
}

const threadSummaryLabel = computed<string | null>(() => {
  const t = props.message.thread;
  if (t === null || t === undefined) return null;
  const n = Math.max(t.reply_count, t.source_reply_count ?? 0);
  let label = `${n} ${pluralReplies(n)}`;
  if (t.last_reply_ts !== null) label += ` · последний ${formatTime(t.last_reply_ts)}`;
  return label;
});

// Ф14-7 B1: новые ответы подписанного треда — отдельной яркой меткой рядом со сводкой, цветом счётчика
// беседы (красный, если у беседы есть непрочитанное «реагирую», иначе серый).
const threadNewCount = computed<number>(() => {
  const t = props.message.thread;
  if (t === null || t === undefined || !t.subscribed) return 0;
  return t.unread > 0 ? t.unread : 0;
});
const threadNewReact = computed<boolean>(
  () => (store.state.sync.channelsById[props.message.channel_id]?.unread_react ?? 0) > 0,
);

function openParentThread(): void {
  if (props.message.ext_id === null) return;
  void openThreadHere(props.message.channel_id, props.message.ext_id);
}

// --- Ф8-4 B4: «Удалить у себя» (у источника сообщение остаётся) — с подтверждением, строка пропадает. ------
// Ревью Ф8-4 (П6): у родителя треда вместе с ним скрываются и ответы — их число в подтверждении.
// Ф14-8 П1: пока у сообщения есть операция (`op`), «Удалить у себя» недоступна (ядро ответит 409 `op_pending`).
function onHideClick(): void {
  closeDeleteMenu();
  if (op.value !== null) return;
  void confirmAndHideMessages([props.message.id], props.message.thread?.reply_count ?? 0);
}

// --- Ф14-8 (раздел D, П1, П6): правка и «удалить у всех» своего сообщения Telegram/Slack. -----------------
const channel = computed(() => store.state.sync.channelsById[props.message.channel_id]);
const editable = computed(() => canEdit(props.message, channel.value));
// Ф40-4 A1: беседа Mattermost — текст сообщения Markdown (лента, тред, история правок).
const markdownText = computed(() => isMarkdownSource(channel.value?.source));
const revocable = computed(() => canRevoke(props.message, channel.value));
const op = computed(() => props.message.op ?? null);
const opEditQueued = computed(() => op.value?.kind === 'edit' && op.value.state === 'queued');
const opDeleteQueued = computed(() => op.value?.kind === 'delete' && op.value.state === 'queued');
const opFailed = computed(() => op.value?.state === 'failed');
const opFailedLabel = computed(() => {
  const current = op.value;
  if (current === null || current.state !== 'failed') return null;
  const head = current.kind === 'edit' ? 'Не изменено' : 'Не удалено у всех';
  return `${head}: ${opFailureReason(current)}`;
});
/** Строка ошибки последнего запроса отмены/повтора/удаления у всех (под сообщением). */
const opError = ref<string | null>(null);
const opBusy = ref(false);

// Текст в строке: ждущая правка — её текст (метка «сохраняется…»), иначе — правда источника.
const shownText = computed(() => {
  const current = op.value;
  if (current !== null && current.kind === 'edit' && current.state === 'queued') return current.text ?? '';
  // Ф27-1: своё сообщение Slack до эха — `<@U…>` показан `@Именем` (`mention_names` у строки с `outbox_id`).
  return props.message.text === null ? null : showMentions(props.message.text, props.message.mention_names);
});
const shownEntities = computed(() => (opEditQueued.value ? null : props.message.entities_json));

const editing = ref(false);
const draft = ref('');
const editError = ref<string | null>(null);
const saving = ref(false);
const editField = ref<HTMLTextAreaElement | null>(null);
const saveButton = ref<HTMLButtonElement | null>(null);
const canSave = computed(
  () => !saving.value && draft.value.trim() !== '' && draft.value !== (props.message.text ?? ''),
);
const editRows = computed(() => Math.min(12, Math.max(2, draft.value.split('\n').length)));
const editWarning = computed(() =>
  editLosesEntities(channel.value?.source, props.message.text, props.message.entities_json),
);

function startEdit(): void {
  const current = op.value;
  draft.value = (current?.kind === 'edit' ? current.text : props.message.text) ?? '';
  editError.value = null;
  editing.value = true;
  void nextTick(() => editField.value?.focus());
}

function cancelEdit(): void {
  editing.value = false;
  editError.value = null;
}

async function saveEdit(): Promise<void> {
  if (!canSave.value) return;
  saving.value = true;
  editError.value = null;
  try {
    await store.editMessage(props.message.id, draft.value);
    editing.value = false;
  } catch (err) {
    console.error('edit message failed', err);
    editError.value = opRequestErrorText(err);
  } finally {
    saving.value = false;
  }
}

function onEditKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault();
    cancelEdit();
    return;
  }
  onSubmitKey(event, saveButton.value);
}

async function runOp(fn: () => Promise<unknown>, what: string): Promise<void> {
  opBusy.value = true;
  opError.value = null;
  try {
    await fn();
  } catch (err) {
    console.error(`${what} failed`, err);
    opError.value = opRequestErrorText(err);
  } finally {
    opBusy.value = false;
  }
}

function onRevokeClick(): void {
  closeDeleteMenu();
  if (!window.confirm(REVOKE_CONFIRM)) return;
  void runOp(() => store.revokeMessages([props.message.id]), 'revoke');
}

function onCancelOp(): void {
  void runOp(() => store.cancelMessageOps([props.message.id]), 'cancel op');
}

function onRetryOp(): void {
  const current = op.value;
  if (current === null) return;
  if (current.kind === 'edit') {
    void runOp(() => store.editMessage(props.message.id, current.text ?? ''), 'retry edit');
  } else {
    void runOp(() => store.revokeMessages([props.message.id]), 'retry revoke');
  }
}

// Меню «Удалить» у своего сообщения: «у себя» и «у всех». Esc и клик мимо закрывают.
const deleteMenuOpen = ref(false);
const deleteMenuEl = ref<HTMLElement | null>(null);
const deleteButtonEl = ref<HTMLElement | null>(null);

usePopup({ open: deleteMenuOpen, button: () => deleteButtonEl.value, panel: () => deleteMenuEl.value });

function closeDeleteMenu(): void {
  deleteMenuOpen.value = false;
}

function toggleDeleteMenu(): void {
  deleteMenuOpen.value = !deleteMenuOpen.value;
}


// --- Ф8-4 C4: «изменено» кликабельно, если у сообщения есть прежние версии (`edit_count` — их число) —
// под сообщением от старой к новой: время + текст тем же рендером, что само сообщение. -------------------
const editCount = computed(() => props.message.edit_count ?? 0);
const editsOpen = ref(false);
const edits = ref<MessageEdit[] | null>(null);

async function loadEdits(): Promise<void> {
  try {
    edits.value = await store.loadEdits(props.message.id);
  } catch (err) {
    console.error('edits load failed', err);
    edits.value = [];
  }
}

function toggleEdits(): void {
  editsOpen.value = !editsOpen.value;
  if (editsOpen.value) void loadEdits();
}

watch(editCount, () => {
  if (editsOpen.value) void loadEdits();
});

function formatEditTime(ts: number): string {
  return new Date(ts).toLocaleString([], {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}
</script>

<template>
  <li
    class="message-row"
    :class="{
      mine: message.is_me,
      'message-row--event': message.kind === 'event',
      'message-row--deleted': message.deleted,
    }"
    :data-message-id="message.id"
    :tabindex="message.kind === 'event' ? undefined : 0"
  >
    <!-- Ф8-4 П6: родитель треда, удалённый у себя в хабе, — заглушка без содержимого и без кнопок. -->
    <p
      v-if="message.hidden"
      class="message-hidden"
    >
      удалено у себя
    </p>
    <!-- Спека 1d-1, п.6: служебные (kind='event') — серой строкой по центру, без отправителя. -->
    <p
      v-else-if="message.kind === 'event' && forward?.auto"
      class="event-text channel-post"
    >
      Пост канала «{{ forward.label }}»
      <button
        type="button"
        class="link-button channel-post-toggle"
        @click="postShown = !postShown"
      >
        {{ postShown ? 'скрыть' : 'показать' }}
      </button>
      <span
        v-if="postShown && message.text !== null"
        class="channel-post-text"
      >
        <component
          :is="MessageText"
          :text="message.text"
          :entities-json="message.entities_json"
        />
      </span>
    </p>
    <p
      v-else-if="message.kind === 'event'"
      class="event-text"
    >
      <component
        :is="MessageText"
        v-if="message.text !== null"
        :text="message.text"
        :entities-json="message.entities_json"
      />
    </p>
    <template v-else>
      <div class="message-meta">
        <button
          v-if="message.sender && senderClickable(message)"
          type="button"
          class="sender-name sender-name--link"
          title="Карточка человека"
          @click="openPersonOfContact(message.sender.id)"
        >
          {{
            message.sender.name ?? message.sender.handle ?? '(без имени)'
          }}
        </button>
        <span
          v-else-if="message.sender"
          class="sender-name"
        >{{
          message.sender.name ?? message.sender.handle ?? '(без имени)'
        }}</span>
        <span
          v-if="message.sender && message.is_bot"
          class="bot-mark"
          title="Сообщение бота"
        >бот</span>
        <time class="message-ts">{{ formatTime(message.ts) }}</time>
        <!-- Ф37-1 C1: ушло без подтверждения от сессии с ролью — роль только текстом -->
        <span
          v-if="message.outbox_auto_role || message.outbox_auto_session != null"
          class="auto-role-mark"
          title="Отправлено без подтверждения сессией с этой ролью"
        >агент: {{ message.outbox_auto_role ?? '' }}{{ message.outbox_auto_session != null ? ' · автопилот' : '' }}</span>
        <!-- Ф39 C: черновик положила сессия с ролью, подтвердил владелец -->
        <span
          v-else-if="message.outbox_author_role"
          class="auto-role-mark"
          title="Черновик положила сессия с этой ролью"
        >роль: {{ message.outbox_author_role }}</span>
        <!-- Ф63-1: сработало правило входящих проекта; название — чужой ввод, только текстом -->
        <span
          v-for="id in message.rule_hits ?? []"
          :key="`rule-${id}`"
          class="rule-mark"
          :title="ruleTitle(id)"
        >правило #{{ id }}</span>
        <span
          v-if="peerRead !== null"
          class="peer-read-mark"
          :class="{ 'peer-read-mark--read': peerRead.mark === 'read' }"
          :title="peerRead.title"
          :aria-label="peerRead.title"
        >{{ peerRead.mark === 'read' ? '✓✓' : '✓' }}</span>
        <span
          v-if="message.deleted"
          class="deleted-mark"
        >удалено</span>
        <button
          v-if="editCount > 0"
          type="button"
          class="edited-mark edited-toggle"
          :aria-expanded="editsOpen"
          @click="toggleEdits"
        >
          изменено
        </button>
        <span
          v-else-if="message.edited_ts !== null"
          class="edited-mark"
        >изменено</span>
        <!-- Спека 2e, раздел «Лента канала»: broadcast — в ленте с пометкой «из треда». -->
        <button
          v-if="message.thread_broadcast"
          type="button"
          class="thread-broadcast-tag"
          @click="openBroadcastThread"
        >
          из треда
        </button>
        <span class="message-actions">
          <button
            v-if="canReply"
            type="button"
            class="reply-button"
            @click="onReplyClick"
          >
            Ответить
          </button>
          <button
            v-if="canWriteAuthor"
            type="button"
            class="reply-button write-author-button"
            :disabled="writeAuthorBusy"
            title="Личное сообщение автору (черновик, уйдёт после твоего подтверждения)"
            @click="onWriteAuthor"
          >
            Написать автору
          </button>
          <button
            v-if="canReact"
            ref="reactButtonEl"
            type="button"
            class="react-button"
            :title="reactButtonTitle"
            aria-label="Реакция"
            :aria-expanded="pickerOpen"
            @click="togglePicker"
          >
            😊+
          </button>
          <button
            v-if="editable && !editing"
            type="button"
            class="edit-button"
            @click="startEdit"
          >
            Изменить
          </button>
          <!-- Ф14-8: у своего сообщения Telegram/Slack «Удалить» — меню «у себя» / «у всех»; у прочих — как было. -->
          <button
            v-if="revocable"
            ref="deleteButtonEl"
            type="button"
            class="hide-button delete-menu-button"
            aria-haspopup="menu"
            :aria-expanded="deleteMenuOpen"
            @click="toggleDeleteMenu"
          >
            Удалить ▾
          </button>
          <button
            v-else
            type="button"
            class="hide-button"
            title="Удалить из хаба (в Telegram/Slack/Mattermost останется)"
            :disabled="op !== null"
            @click="onHideClick"
          >
            Удалить у себя
          </button>
        </span>
      </div>
      <div
        v-if="deleteMenuOpen && revocable"
        ref="deleteMenuEl"
        class="delete-menu"
        role="menu"
      >
        <button
          type="button"
          class="delete-menu-item hide-self-item"
          role="menuitem"
          title="В Telegram/Slack/Mattermost сообщение останется"
          :disabled="op !== null"
          @click="onHideClick"
        >
          Удалить у себя (только в хабе)
        </button>
        <button
          type="button"
          class="delete-menu-item revoke-item"
          role="menuitem"
          :disabled="opDeleteQueued"
          @click="onRevokeClick"
        >
          Удалить у всех
        </button>
      </div>
      <div
        v-if="pickerOpen && canReact"
        ref="pickerEl"
        class="reaction-picker"
        role="menu"
        aria-label="Реакции"
      >
        <button
          v-for="emoji in quickReactions"
          :key="emoji"
          type="button"
          class="reaction-pick"
          role="menuitem"
          @click="onPick(emoji)"
        >
          {{ emoji }}
        </button>
        <button
          v-if="moreReactions.length > 0"
          type="button"
          class="reaction-more"
          title="Все реакции"
          aria-label="Все реакции"
          :aria-expanded="pickerExpanded"
          @click="pickerExpanded = !pickerExpanded"
        >
          {{ pickerExpanded ? '–' : '+' }}
        </button>
        <div
          v-if="pickerExpanded"
          class="reaction-grid"
        >
          <button
            v-for="emoji in moreReactions"
            :key="emoji"
            type="button"
            class="reaction-pick reaction-pick--more"
            :class="{ 'reaction-pick--name': isNamedReaction(emoji) }"
            :title="isNamedReaction(emoji) ? emoji : undefined"
            role="menuitem"
            @click="onPick(emoji)"
          >
            {{ emoji }}
          </button>
        </div>
      </div>
      <p
        v-if="mailTo"
        class="forwarded mail-to"
        :title="mailTo.title"
      >
        кому: {{ mailTo.label }}
      </p>
      <p
        v-if="forward"
        class="forwarded"
      >
        переслано от {{ forward.label }}
      </p>
      <p
        v-if="!deletedNothingToShow && message.reply_to_ext_id !== null"
        class="reply-quote"
        :class="{ clickable: replyQuoteTarget !== null }"
        @click="scrollToReplyTarget"
      >
        ↪ {{ replyQuoteLabel }}
      </p>
      <p
        v-if="deletedNothingToShow"
        class="message-deleted"
      >
        сообщение удалено
      </p>
      <!-- Спека 2e, раздел «Лента канала»: `purged` — вместо текста серым, ретенция work-БД (план 7). -->
      <p
        v-else-if="message.purged"
        class="message-purged"
      >
        текст удалён по сроку хранения (90 дней)
      </p>
      <!-- Ф14-8 раздел D: правка на месте — поле вместо текста. -->
      <div
        v-else-if="editing"
        class="message-edit"
      >
        <textarea
          ref="editField"
          v-model="draft"
          class="message-edit__field"
          :rows="editRows"
          aria-label="Новый текст сообщения"
          @keydown="onEditKey"
        />
        <DictateButton />
        <p
          v-if="editWarning"
          class="message-edit__warning"
        >
          {{ ENTITIES_LOST_WARNING }}
        </p>
        <p
          v-if="editError !== null"
          class="message-edit__error"
          role="alert"
        >
          {{ editError }}
        </p>
        <div class="message-edit__actions">
          <button
            ref="saveButton"
            type="button"
            class="message-edit__save"
            :disabled="!canSave"
            @click="saveEdit"
          >
            Сохранить
          </button>
          <button
            type="button"
            class="message-edit__cancel"
            @click="cancelEdit"
          >
            Отмена
          </button>
        </div>
      </div>
      <component
        :is="MessageText"
        v-else-if="shownText !== null"
        :text="shownText"
        :entities-json="shownEntities"
        :markdown="markdownText"
      />
      <!-- Ф14-8 раздел D: состояние операции. -->
      <p
        v-if="opEditQueued || opDeleteQueued"
        class="msg-op-line msg-op-line--queued"
        role="status"
      >
        <span class="msg-op-label">{{ opEditQueued ? 'сохраняется…' : 'удаляется у всех…' }}</span>
        <button
          type="button"
          class="msg-op-cancel"
          :disabled="opBusy"
          @click="onCancelOp"
        >
          Отменить
        </button>
      </p>
      <p
        v-else-if="opFailed"
        class="msg-op-line msg-op-line--failed"
        role="status"
      >
        <span class="msg-op-label">{{ opFailedLabel }}</span>
        <button
          type="button"
          class="msg-op-retry"
          :disabled="opBusy"
          @click="onRetryOp"
        >
          Повторить
        </button>
        <button
          type="button"
          class="msg-op-dismiss"
          :disabled="opBusy"
          @click="onCancelOp"
        >
          Скрыть
        </button>
      </p>
      <p
        v-if="writeAuthorError !== null"
        class="msg-op-error"
        role="alert"
      >
        {{ writeAuthorError }}
      </p>
      <p
        v-if="opError !== null"
        class="msg-op-error"
        role="alert"
      >
        {{ opError }}
      </p>
      <ol
        v-if="editsOpen"
        class="edit-history"
        aria-label="Прежние версии"
      >
        <li
          v-if="edits === null"
          class="loading-hint"
        >
          Загрузка…
        </li>
        <li
          v-for="(edit, i) in edits ?? []"
          :key="i"
          class="edit-version"
        >
          <time class="edit-time">{{ formatEditTime(edit.replaced_ts) }}</time>
          <component
            :is="MessageText"
            v-if="edit.text !== null"
            :text="edit.text"
            :entities-json="edit.entities_json"
            :markdown="markdownText"
          />
        </li>
      </ol>
      <AttachmentList :messages="[message]" />
      <!-- Ф44-1b: инлайн-кнопки бота. Нажатие — кусок 44-1c (`store/buttons.ts`). -->
      <MessageButtons
        v-if="message.buttons && message.buttons.rows.length > 0"
        :buttons="message.buttons"
        :busy="buttonState(message.id).busy"
        :waiting="buttonState(message.id).waiting"
        :lines="buttonState(message.id).lines"
        :pending="buttonState(message.id).pending"
        @press="(p) => pressButton(message.id, p.row, p.col, p.text)"
      />
      <!-- Ф23-3: просмотры поста канала — в одной строке с реакциями. -->
      <div
        v-if="message.views"
        class="msg-counters"
      >
        <ReactionsLine
          :reactions="reactions"
          :clickable="canReact"
          @toggle="react"
        />
        <PostViews :views="message.views" />
      </div>
      <ReactionsLine
        v-else
        :reactions="reactions"
        :clickable="canReact"
        @toggle="react"
      />
      <p
        v-if="reactionErrorText !== null"
        class="reaction-error"
        role="status"
      >
        {{ reactionErrorText }}
      </p>
      <OutboxStatusBadge
        v-if="message.outbox_id !== null && message.outbox_status !== null"
        :outbox-id="message.outbox_id"
        :status="message.outbox_status"
        :text="message.text ?? ''"
        :mention-names="message.mention_names"
        :paced="message.outbox_paced ?? false"
        :not-before-ts="message.outbox_not_before_ts ?? null"
        :send-at-ts="message.outbox_send_at_ts ?? null"
        :cancel-if-reply="message.outbox_cancel_if_reply ?? false"
        :held="message.outbox_held ?? null"
      />
      <!-- Спека 2e, раздел «Лента канала»: сводка треда под родителем (message.thread не null); Ф14-7 B1:
           «K новых» — отдельной меткой рядом. -->
      <div
        v-if="threadSummaryLabel !== null"
        class="thread-summary-line"
      >
        <button
          type="button"
          class="thread-summary"
          @click="openParentThread"
        >
          {{ threadSummaryLabel }}
        </button>
        <button
          v-if="threadNewCount > 0"
          type="button"
          class="badge thread-summary-new"
          :class="threadNewReact ? 'badge-react' : 'badge-quiet'"
          @click="openParentThread"
        >
          {{ threadNewCount }} новых
        </button>
      </div>
    </template>
  </li>
</template>
