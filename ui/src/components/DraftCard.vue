<script setup lang="ts">
// Ф8-6 C1/C2: черновик (`pending_approval`) — на главной (с адресом и «Открыть беседу») и закреплённым над
// полем ввода беседы. Текст черновика, превью и имена — недоверенный ввод: только текстовая интерполяция Vue.
// «Отправить» — `approve` (с правленым текстом, если правили), «Отклонить» — `cancel`.
// Ф12-2 C: черновик по графику — пометка «по графику» и флажок выбора (`selectable`) для «Подтвердить выбранные»;
// подтверждённый по графику (`queued`) — «уйдёт в ЧЧ:ММ» / «завтра в ЧЧ:ММ», «Отправить сейчас» (24.10) и «Отменить».
// Ф14-3 D: файлы черновика видны сразу, без раскрытия (миниатюра у картинок, имя текстом, размер, «открыть»);
// у черновика сессии (любой bearer, `origin='api'`: и hub-wake, и `hub-all`) — «Claude приложил файлы: N»; «Отправить» — и при пустой подписи, если файлы
// есть; флажка пачки у черновика с файлами нет (подтверждается только из карточки).
// Номер `#id` (id строки outbox) — всегда в шапке: сессии пишут владельцу «черновик #N».
import { computed, inject, ref } from 'vue';

import * as api from '../api/client';
import type { OutboxListItem } from '../api/types';
import { formatSize } from '../render/attachments';
import { store } from '../store/instance';
import { IN_BOARD_COLUMN } from '../util/boardCtx';
import { addressLabel, draftAuthor, draftTargetLabel, formatTime } from '../util/home';
import { cancelIfReplyMark, isSendAtPassed, sendAtLabel, slotLabel } from '../util/pace';
import { botGuardNote } from '../util/presses';
import { type MentionMap, pruneMap, showMentions, toLabels, toWire } from '../util/mentions';
import { onSubmitKey, showSubmitHint, submitKeyLabel } from '../util/submitKeys';
import { isMarkdownSource } from '../util/textFormat';
import ModelText from './ModelText.vue';
import DictateButton from './DictateButton.vue';

const inBoardColumn = inject(IN_BOARD_COLUMN, false);

const props = withDefaults(
  defineProps<{ item: OutboxListItem; showAddress?: boolean; compact?: boolean; selectable?: boolean }>(),
  {
    showAddress: true,
    compact: false,
    selectable: false,
  },
);

// Ф20-4 C4: в окне черновика (`DraftModal`) карточка сообщает `done` — отправлен, отклонён или открыта беседа.
const emit = defineEmits<{ done: [] }>();

const editing = ref(false);
const editField = ref<HTMLTextAreaElement | null>(null);
// Ф27-1: Slack `<@U…>` показан `@Имя` (`mention_names` ядра); в поле правки тоже `@Имя`, при отправке — обратно `<@id>`.
const editText = ref(toLabels(props.item.text ?? '', props.item.mention_names).text);
let editMap: MentionMap = toLabels(props.item.text ?? '', props.item.mention_names).map;
const shownText = computed(() => showMentions(props.item.text ?? '', props.item.mention_names));
// Ф40-4 A1: черновик в беседу Mattermost — текст показан как Markdown (в поле правки — сырой).
const markdown = computed(() => isMarkdownSource(props.item.source ?? store.state.sync.channelsById[props.item.channel_id]?.source));
const busy = ref(false);
const sendButton = ref<HTMLButtonElement | null>(null);
const keyLabel = submitKeyLabel();
const showHint = showSubmitHint(); // Ф11-4 И5б
const actionError = ref<string | null>(null);

const target = computed(() => draftTargetLabel(props.item));
const author = computed(() => draftAuthor(props.item));
const isClaude = computed(() => author.value.startsWith('Claude'));
const guardNote = computed(() => botGuardNote(props.item.bot_guard));
const scheduled = computed(() => props.item.status === 'queued');
const slot = computed(() =>
  scheduled.value && typeof props.item.not_before_ts === 'number' ? slotLabel(props.item.not_before_ts) : null,
);
const selected = computed(() => store.isDraftSelected(props.item.id));
// Ф30-1 C: черновик сессии «к точному времени» — «к 10:00, 5 окт» до подтверждения; время прошло (409) —
// строка «время прошло» и «Отправить сейчас».
const sendAt = computed(() =>
  typeof props.item.send_at_ts === 'number' ? sendAtLabel(props.item.send_at_ts) : null,
);
// Ф30-2: догон с `cancel_if_reply` — к пометке времени « · не уйдёт, если ответят».
const replyMark = computed(() => cancelIfReplyMark(props.item.send_at_ts, props.item.cancel_if_reply));
const passed = ref<string | null>(null);
const files = computed(() => props.item.files ?? []);
const hasFiles = computed(() => files.value.length > 0);
/** Пустой текст — подпись к файлам: «Отправить» доступна, если файлы есть (Ф14-3 D2). */
const canSend = computed(() => {
  const text = editing.value ? editText.value : (props.item.text ?? '');
  return hasFiles.value || text.trim() !== '';
});

function startEdit(): void {
  const labels = toLabels(props.item.text ?? '', props.item.mention_names);
  editText.value = labels.text;
  editMap = labels.map;
  editing.value = true;
}

function describeError(err: unknown): string {
  return err instanceof api.ApiError ? err.message : 'Не удалось выполнить действие. Проверьте соединение.';
}

async function send(now = false): Promise<void> {
  const text = editing.value ? editText.value : (props.item.text ?? '');
  if (!canSend.value || busy.value) return;
  busy.value = true;
  actionError.value = null;
  passed.value = null;
  const wire = editing.value ? toWire(text, pruneMap(text, editMap)) : undefined;
  try {
    if (now) await store.approveOutboxNow(props.item.id, wire);
    else await store.approveOutbox(props.item.id, wire);
    editing.value = false;
    emit('done');
  } catch (err) {
    if (isSendAtPassed(err)) passed.value = describeError(err);
    else actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}

async function reject(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  actionError.value = null;
  try {
    await store.cancelOutbox(props.item.id);
    emit('done');
  } catch (err) {
    actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}

/** 24.10: «Отправить сейчас» у подтверждённой строки — график и срок снимаются, уходит при ближайшей выдаче. */
async function releaseNow(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  actionError.value = null;
  try {
    await store.sendOutboxNow(props.item.id);
    emit('done');
  } catch (err) {
    actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}

async function openChannel(): Promise<void> {
  await store.openChannelAt(props.item.project_id, props.item.channel_id, { outboxId: props.item.id });
  // Черновик в тред — строка черновика живёт в панели треда, не в ленте канала.
  if (props.item.thread) await (inBoardColumn ? store.openThreadInProject(props.item.channel_id, props.item.thread.ext_id) : store.openThread(props.item.channel_id, props.item.thread.ext_id));
  emit('done');
}
</script>

<template>
  <article
    :id="`draft-${item.id}`"
    class="draft-card"
    :class="{ 'draft-card--compact': compact, 'draft-card--scheduled': scheduled }"
    :data-outbox-id="item.id"
  >
    <header class="draft-card-head">
      <span
        v-if="selectable && !scheduled && hasFiles"
        class="draft-select-files"
        title="с файлами — только из карточки"
      >с файлами</span>
      <input
        v-else-if="selectable && !scheduled"
        type="checkbox"
        class="draft-select"
        aria-label="Выбрать черновик"
        :checked="selected"
        :disabled="busy"
        @change="store.toggleDraftSelection(item.id)"
      >
      <span
        class="draft-card-num"
        title="Номер черновика — по нему на черновик ссылаются сессии Claude"
      >#{{ item.id }}</span>
      <span
        v-if="compact"
        class="draft-card-label"
      >{{ isClaude ? 'Черновик Claude' : 'Черновик' }}:</span>
      <span
        v-if="showAddress"
        class="draft-card-address"
      >{{ addressLabel(item.project_name, item.channel_title) }}</span>
      <span
        v-if="!compact"
        class="draft-card-author"
      >{{ author }}</span>
      <span
        v-if="!compact"
        class="draft-card-time"
      >{{ formatTime(item.created_ts) }}</span>
      <span
        v-if="slot !== null"
        class="draft-card-slot"
      >{{ slot }}{{ replyMark }}</span>
      <span
        v-else-if="item.paced"
        class="draft-card-paced"
        title="После подтверждения уйдёт по графику аккаунта: в окне и не больше суточного лимита"
      >по графику</span>
      <span
        v-else-if="sendAt !== null"
        class="draft-card-paced draft-card-send-at"
        title="После подтверждения уйдёт в это время; если хаб опоздает больше чем на 2 ч — не уйдёт"
      >{{ sendAt }}{{ replyMark }}</span>
      <span
        v-if="guardNote"
        class="draft-card-guard"
        data-bot-guard
      >{{ guardNote }}</span>
      <span
        v-if="item.author_role"
        class="draft-card-role"
        title="черновик положила сессия с этой ролью"
      >{{ item.author_role }}</span>
    </header>
    <p
      v-if="target !== null"
      class="draft-card-target"
    >
      {{ target }}
    </p>
    <textarea
      v-if="editing && !scheduled"
      ref="editField"
      v-model="editText"
      class="draft-card-edit"
      rows="3"
      aria-label="Текст черновика"
      enterkeyhint="enter"
      :disabled="busy"
      @keydown="onSubmitKey($event, sendButton)"
    />
    <div
      v-else-if="markdown"
      class="draft-card-text draft-card-text--md"
    >
      <ModelText :text="shownText" />
    </div>
    <p
      v-else
      class="draft-card-text"
    >
      {{ shownText }}
    </p>
    <DictateButton
      v-if="editing && !scheduled"
      :target="editField"
    />
    <div
      v-if="hasFiles"
      class="draft-card-files"
    >
      <p
        v-if="item.origin === 'api'"
        class="draft-card-files-note"
      >
        Claude приложил файлы: {{ files.length }}
      </p>
      <ul class="draft-card-file-list">
        <li
          v-for="file in files"
          :key="file.media_id"
          class="draft-card-file"
        >
          <img
            v-if="file.kind === 'photo'"
            class="draft-card-file-thumb"
            :src="api.mediaUrl(file.media_id)"
            alt=""
            loading="lazy"
          >
          <span class="draft-card-file-name">{{ file.name ?? 'файл' }}</span>
          <span
            v-if="typeof file.size === 'number'"
            class="draft-card-file-size"
          >{{ formatSize(file.size) }}</span>
          <a
            class="draft-card-file-open"
            :href="api.mediaUrl(file.media_id)"
            target="_blank"
            rel="noopener noreferrer"
          >открыть</a>
        </li>
      </ul>
    </div>
    <div
      v-if="scheduled"
      class="draft-card-actions"
    >
      <button
        type="button"
        class="draft-release-now"
        :disabled="busy"
        title="Уйдёт сразу — без графика и срока"
        @click="releaseNow"
      >
        Отправить сейчас
      </button>
      <button
        type="button"
        class="draft-reject"
        :disabled="busy"
        @click="reject"
      >
        Отменить
      </button>
      <button
        v-if="showAddress"
        type="button"
        class="draft-open"
        @click="openChannel"
      >
        Открыть беседу
      </button>
    </div>
    <div
      v-else
      class="draft-card-actions"
    >
      <button
        ref="sendButton"
        type="button"
        class="draft-send"
        :title="editing ? keyLabel : undefined"
        :disabled="busy || !canSend"
        @click="send()"
      >
        Отправить
      </button>
      <span
        v-if="editing && showHint"
        class="submit-hint"
        aria-hidden="true"
      >{{ keyLabel }}</span>
      <button
        v-if="!editing"
        type="button"
        class="draft-edit"
        :disabled="busy"
        @click="startEdit"
      >
        Править
      </button>
      <button
        v-else
        type="button"
        class="draft-edit-cancel"
        :disabled="busy"
        @click="editing = false"
      >
        Не править
      </button>
      <button
        type="button"
        class="draft-reject"
        :disabled="busy"
        @click="reject"
      >
        Отклонить
      </button>
      <button
        v-if="showAddress"
        type="button"
        class="draft-open"
        @click="openChannel"
      >
        Открыть беседу
      </button>
    </div>
    <p
      v-if="passed !== null"
      class="draft-card-error draft-send-at-passed"
      role="alert"
    >
      {{ passed }}
      <button
        type="button"
        class="draft-send-now"
        :disabled="busy || !canSend"
        @click="send(true)"
      >
        Отправить сейчас
      </button>
    </p>
    <p
      v-if="actionError"
      class="draft-card-error"
      role="alert"
    >
      {{ actionError }}
    </p>
  </article>
</template>
