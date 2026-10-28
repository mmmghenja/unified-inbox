<script setup lang="ts">
// Альбом (спека 1d-1, п.2): подряд идущие сообщения одного отправителя с одинаковым `grouped_id` — одна
// строка ленты: общая подпись (первая непустая по порядку показа) + вложения ВСЕХ сообщений альбома.
// Группировка сама по себе — в `store/feedRows.ts` (чистая функция); этот компонент только показывает уже
// готовую группу. `messages` приходит из реактивного стора — правка/удаление любого сообщения альбома меняет
// исходный массив, `computed` ниже пересчитываются сами, без ручной подписки.
import { computed, ref } from 'vue';

import type { Message } from '../api/types';
import type { ParsedAttachment } from '../render/attachments';
import { parseAttachments } from '../render/attachments';
import { store } from '../store/instance';
import { openPersonOfContact, senderClickable } from '../store/people';
import { confirmAndHideMessages } from '../util/hide';
import { REVOKE_CONFIRM, canRevoke, opFailureReason, opRequestErrorText } from '../util/msgOps';
import { usePopup } from '../util/popup';
import { pressButton } from '../store/buttons';
import { buttonState } from '../store/pressView';
import AttachmentList from './AttachmentList.vue';
import MessageButtons from './MessageButtons.vue';
import MessageText from './MessageText';
import PostViews from './PostViews.vue';

const props = defineProps<{ messages: Message[] }>();

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

const firstMessage = computed(() => props.messages[0]!);
// Ф23-3: у каждой части альбома канала свой счётчик — показываем наибольший.
const albumViews = computed(() => {
  let best: Message['views'] = null;
  for (const m of props.messages) {
    if (m.views && (best === null || best === undefined || m.views.views > best.views)) best = m.views;
  }
  return best ?? null;
});
const lastMessage = computed(() => props.messages[props.messages.length - 1]!);
// Ф8-1 C2: удалённые сообщения альбома остаются видны (подпись и вложения), как в `MessageRow.vue`: альбом,
// удалённый целиком, приглушён с пометкой «удалено» у времени, частично удалённый — «удалено K из N».
// Удалённое без подписи, но с вложением (фото альбома) — вложение остаётся видно (решение архитектора 24.09).
const deletedCount = computed(() => props.messages.filter((m) => m.deleted).length);
const allDeleted = computed(() => deletedCount.value === props.messages.length);

// Первая непустая подпись в порядке показа; живые сообщения — в приоритете перед удалёнными.
const caption = computed(() => {
  const live = props.messages.filter((m) => !m.deleted);
  for (const m of [...live, ...props.messages]) {
    if (m.text !== null && m.text !== '') return m;
  }
  return null;
});

const attachments = computed<ParsedAttachment[]>(() => {
  const result: ParsedAttachment[] = [];
  for (const m of props.messages) {
    result.push(...parseAttachments(m.attachments_json));
  }
  return result;
});

// Показать нечего (удалено, и нет ни подписи, ни вложений) — «сообщение удалено», как раньше.
const nothingToShow = computed(
  () => allDeleted.value && caption.value === null && attachments.value.length === 0,
);

// Ф8-4 П11: «Удалить у себя» на строке альбома скрывает все его части (`DELETE` на каждую).
// Ф14-8 П1: пока у какой-то части есть операция (`op`), «Удалить у себя» недоступна (ядро ответит 409).
function onHideClick(): void {
  closeDeleteMenu();
  if (hasOps.value) return;
  void confirmAndHideMessages(props.messages.map((m) => m.id));
}

// --- Ф14-8 (раздел D, П2): «удалить у всех» своего альбома — одним запросом по неудалённым частям; `op` частей
// показывается сводно. «Изменить» у альбома нет. ---------------------------------------------------------------
const liveParts = computed(() => props.messages.filter((m) => !m.deleted));
const revocable = computed(() => {
  const channel = store.state.sync.channelsById[firstMessage.value.channel_id];
  return liveParts.value.length > 0 && liveParts.value.every((m) => canRevoke(m, channel));
});
const hasOps = computed(() => props.messages.some((m) => (m.op ?? null) !== null));
const queuedParts = computed(() => props.messages.filter((m) => m.op?.kind === 'delete' && m.op.state === 'queued'));
const failedParts = computed(() => props.messages.filter((m) => m.op?.kind === 'delete' && m.op.state === 'failed'));
const failedLabel = computed(() => {
  const first = failedParts.value[0]?.op;
  if (first === undefined || first === null) return null;
  return `Не удалено у всех (${failedParts.value.length} из ${props.messages.length}): ${opFailureReason(first)}`;
});
const opError = ref<string | null>(null);
const opBusy = ref(false);

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
  const ids = liveParts.value.map((m) => m.id);
  void runOp(() => store.revokeMessages(ids), 'revoke album');
}

function onCancelQueued(): void {
  const ids = queuedParts.value.map((m) => m.id);
  void runOp(() => store.cancelMessageOps(ids), 'cancel album op');
}

function onRetryFailed(): void {
  const ids = failedParts.value.map((m) => m.id);
  void runOp(() => store.revokeMessages(ids), 'retry album revoke');
}

function onDismissFailed(): void {
  const ids = failedParts.value.map((m) => m.id);
  void runOp(() => store.cancelMessageOps(ids), 'dismiss album op');
}

// Меню «Удалить»: «у себя» / «у всех». Esc и клик мимо закрывают.
const deleteMenuOpen = ref(false);
const deleteMenuEl = ref<HTMLElement | null>(null);
const deleteButtonEl = ref<HTMLElement | null>(null);

// Ф80-1 A2: общий механизм меню (`util/popup`).
usePopup({ open: deleteMenuOpen, button: () => deleteButtonEl.value, panel: () => deleteMenuEl.value });

function closeDeleteMenu(): void {
  deleteMenuOpen.value = false;
}

function toggleDeleteMenu(): void {
  deleteMenuOpen.value = !deleteMenuOpen.value;
}
</script>

<template>
  <li
    class="message-row album-row"
    :class="{ mine: firstMessage.is_me, 'message-row--deleted': allDeleted }"
    :data-message-id="lastMessage.id"
  >
    <div class="message-meta">
      <button
        v-if="firstMessage.sender && senderClickable(firstMessage)"
        type="button"
        class="sender-name sender-name--link"
        title="Карточка человека"
        @click="openPersonOfContact(firstMessage.sender.id)"
      >
        {{
          firstMessage.sender.name ?? firstMessage.sender.handle ?? '(без имени)'
        }}
      </button>
      <span
        v-else-if="firstMessage.sender"
        class="sender-name"
      >{{
        firstMessage.sender.name ?? firstMessage.sender.handle ?? '(без имени)'
      }}</span>
      <time class="message-ts">{{ formatTime(lastMessage.ts) }}</time>
      <span
        v-if="allDeleted"
        class="deleted-mark"
      >удалено</span>
      <span
        v-else-if="deletedCount > 0"
        class="deleted-mark"
      >удалено {{ deletedCount }} из {{ messages.length }}</span>
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
        :disabled="hasOps"
        @click="onHideClick"
      >
        Удалить у себя
      </button>
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
        :disabled="hasOps"
        @click="onHideClick"
      >
        Удалить у себя (только в хабе)
      </button>
      <button
        type="button"
        class="delete-menu-item revoke-item"
        role="menuitem"
        :disabled="queuedParts.length > 0"
        @click="onRevokeClick"
      >
        Удалить у всех
      </button>
    </div>
    <p
      v-if="nothingToShow"
      class="message-deleted"
    >
      сообщение удалено
    </p>
    <template v-else>
      <component
        :is="MessageText"
        v-if="caption"
        :text="caption.text ?? ''"
        :entities-json="caption.entities_json"
      />
      <AttachmentList :messages="messages" />
      <!-- Ф44-1b: кнопки несёт последнее сообщение альбома. -->
      <MessageButtons
        v-if="lastMessage.buttons && lastMessage.buttons.rows.length > 0"
        :buttons="lastMessage.buttons"
        :busy="buttonState(lastMessage.id).busy"
        :waiting="buttonState(lastMessage.id).waiting"
        :lines="buttonState(lastMessage.id).lines"
        :pending="buttonState(lastMessage.id).pending"
        @press="(p) => pressButton(lastMessage.id, p.row, p.col, p.text)"
      />
      <!-- Ф23-3: альбом поста канала — просмотры одной строкой (наибольшие среди частей). -->
      <p
        v-if="albumViews !== null"
        class="msg-counters"
      >
        <PostViews :views="albumViews" />
      </p>
    </template>
    <p
      v-if="queuedParts.length > 0"
      class="msg-op-line msg-op-line--queued"
      role="status"
    >
      <span class="msg-op-label">удаляется у всех…</span>
      <button
        type="button"
        class="msg-op-cancel"
        :disabled="opBusy"
        @click="onCancelQueued"
      >
        Отменить
      </button>
    </p>
    <p
      v-if="failedLabel !== null"
      class="msg-op-line msg-op-line--failed"
      role="status"
    >
      <span class="msg-op-label">{{ failedLabel }}</span>
      <button
        type="button"
        class="msg-op-retry"
        :disabled="opBusy"
        @click="onRetryFailed"
      >
        Повторить
      </button>
      <button
        type="button"
        class="msg-op-dismiss"
        :disabled="opBusy"
        @click="onDismissFailed"
      >
        Скрыть
      </button>
    </p>
    <p
      v-if="opError !== null"
      class="msg-op-error"
      role="alert"
    >
      {{ opError }}
    </p>
  </li>
</template>
