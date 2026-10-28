<script setup lang="ts">
// Ф20-4 C4/C5 (`docs/architecture.md`): черновик одной строкой — столбец главной и «Ждёт» телефона.
// Черновик: [флажок | «с файлами»] кому · начало текста · «по графику» · «Отправить» «Отклонить» «›».
// Подтверждённый по графику (`queued`): кому · когда уйдёт · «Отправить сейчас» «Отменить» «›». Номер `#id` (у боевых — 13 цифр)
// и «ответ на …» — в подсказке строки и в окне: в строке они съедали всё место под текст. «Кому» — новый адресат
// или название беседы; проект — только при `showProject` («Ждёт» телефона), в столбце он и так известен.
// «Отправить» и «Отклонить» — те же вызовы стора и та же блокировка `busy`, что у `DraftCard`; ошибка — под строкой.
// «›» открывает окно `DraftModal` (там полная карточка); закрылось — фокус возвращается на «›».
// Тексты черновика, имена и превью — недоверенный ввод: только текстовая интерполяция Vue.
import { computed, nextTick, ref } from 'vue';

import * as api from '../api/client';
import type { OutboxListItem } from '../api/types';
import { store } from '../store/instance';
import { addressLabel, draftTargetLabel, newRecipientLabel } from '../util/home';
import { showMentions } from '../util/mentions';
import { cancelIfReplyMark, isSendAtPassed, sendAtLabel, slotLabel } from '../util/pace';
import { botGuardNote } from '../util/presses';
import { draftModal } from '../store/draftModal';
import DraftModal from './DraftModal.vue';

const props = withDefaults(defineProps<{ item: OutboxListItem; selectable?: boolean; showProject?: boolean }>(), {
  selectable: false,
  showProject: false,
});

const busy = ref(false);
const actionError = ref<string | null>(null);
const modalOpen = ref(false);
const openButton = ref<HTMLButtonElement | null>(null);

const guardNote = computed(() => botGuardNote(props.item.bot_guard));
const scheduled = computed(() => props.item.status === 'queued');
const who = computed(() => {
  const recipient = newRecipientLabel(props.item.new_to);
  if (recipient !== null) return recipient;
  return props.showProject
    ? addressLabel(props.item.project_name, props.item.channel_title)
    : (props.item.channel_title ?? '(без названия)');
});
const hint = computed(() =>
  [`#${props.item.id}`, draftTargetLabel(props.item) ?? who.value].join(' · '),
);
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
// Ф27-1: Slack `<@U…>` показан `@Именем` (`mention_names` ядра).
const shownText = computed(() => showMentions(props.item.text ?? '', props.item.mention_names));
const hasFiles = computed(() => (props.item.files ?? []).length > 0);
const canSend = computed(() => hasFiles.value || (props.item.text ?? '').trim() !== '');

function describeError(err: unknown): string {
  return err instanceof api.ApiError ? err.message : 'Не удалось выполнить действие. Проверьте соединение.';
}

async function send(now = false): Promise<void> {
  if (!canSend.value || busy.value) return;
  busy.value = true;
  actionError.value = null;
  passed.value = null;
  try {
    if (now) await store.approveOutboxNow(props.item.id);
    else await store.approveOutbox(props.item.id);
  } catch (err) {
    if (isSendAtPassed(err)) passed.value = describeError(err);
    else actionError.value = describeError(err);
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
  } catch (err) {
    actionError.value = describeError(err);
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
  } catch (err) {
    actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}

function openModal(): void {
  modalOpen.value = true;
}

async function closeModal(): Promise<void> {
  modalOpen.value = false;
  await nextTick();
  if (draftModal.id === null) openButton.value?.focus(); // листание: окно продолжает общий хозяин
}
</script>

<template>
  <div
    class="draft-row-wrap"
    :data-outbox-id="item.id"
  >
    <div
      class="draft-row"
      :class="{ 'draft-row--scheduled': scheduled }"
    >
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
        class="draft-row-who"
        :title="hint"
      >{{ who }}</span>
      <span
        class="draft-card-num draft-card-num--quiet"
        title="Номер черновика — по нему на черновик ссылаются сессии Claude"
      >#{{ item.id }}</span>
      <!-- Ф39 C: роль сессии-автора — какая из ролей проекта положила черновик -->
      <span
        v-if="guardNote"
        class="draft-card-guard"
        data-bot-guard
      >{{ guardNote }}</span>
      <span
        v-if="item.author_role"
        class="draft-card-role draft-row-role"
        title="черновик положила сессия с этой ролью"
      >{{ item.author_role }}</span>
      <span
        v-if="!scheduled"
        class="draft-row-text"
        :title="shownText"
      >{{ shownText }}</span>
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
      <span class="draft-row-actions">
        <template v-if="scheduled">
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
        </template>
        <template v-else>
          <button
            type="button"
            class="draft-send"
            aria-label="Отправить"
            :disabled="busy || !canSend"
            @click="send()"
          >
            <span class="draft-btn-full">Отправить</span><span
              class="draft-btn-short"
              aria-hidden="true"
            >✓</span>
          </button>
          <button
            type="button"
            class="draft-reject"
            aria-label="Отклонить"
            :disabled="busy"
            @click="reject"
          >
            <span class="draft-btn-full">Отклонить</span><span
              class="draft-btn-short"
              aria-hidden="true"
            >✕</span>
          </button>
        </template>
        <button
          ref="openButton"
          type="button"
          class="draft-row-open"
          :aria-label="`Открыть черновик #${item.id}`"
          aria-haspopup="dialog"
          @click="openModal"
        >
          ›
        </button>
      </span>
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
    <DraftModal
      v-if="modalOpen"
      :item="item"
      @close="closeModal"
    />
  </div>
</template>
