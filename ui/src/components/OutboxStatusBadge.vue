<script setup lang="ts">
// Честные статусы исходящего (спека, «Уточнения архитектора»): статус всегда берётся из строки сообщения
// (`outbox_status`, обновляется через /sync) — ни один локальный переход ("отменено") не рисуется по
// оптимистичному предположению; `cancel` может вернуть `unknown`, и следующий /sync покажет именно его.
//
// F7 (приёмка 6b): действия (approve/cancel/resolve) раньше не ловили ошибку — 409/404/сетевой сбой уходили
// необработанным rejection, пользователь ничего не видел. Теперь ошибка показана текстом в плашке. Правка
// черновика (`editText`) синхронизируется с `props.text`, ПОКА пользователь не начал редактировать сам
// (иначе правка, пришедшая через /sync, стирала бы то, что человек уже печатает).
import { computed, ref, watch } from 'vue';

import * as api from '../api/client';
import type { OutboxStatus, ResolveUnknownDecision } from '../api/types';
import { store } from '../store/instance';
import { type MentionMap, pruneMap, toLabels, toWire } from '../util/mentions';
import { cancelIfReplyMark, isSendAtPassed, outboxErrorText, sendAtLabel, slotLabel } from '../util/pace';
import { onSubmitKey, showSubmitHint, submitKeyLabel } from '../util/submitKeys';

// Ф12-2 C: черновик по графику — «ждёт подтверждения · по графику»; подтверждённый — «уйдёт в ЧЧ:ММ» (или
// «завтра в ЧЧ:ММ») и «Отменить», как у любой строки очереди (`cancel` из `queued`).
// Ф30-1 C + П3: черновик сессии «к точному времени» (`sendAtTs`) — «ждёт подтверждения · к 10:00, 5 окт»,
// подтверждённый — «уйдёт в …» и «Отменить»; 409 `send_at_passed` на «Отправить» — «время прошло» и
// «Отправить сейчас»; `failed` по опозданию — причина простыми словами.
const props = withDefaults(
  defineProps<{
    outboxId: number;
    status: OutboxStatus;
    text: string;
    paced?: boolean;
    notBeforeTs?: number | null;
    /** Ф30-1: время `send_at` черновика сессии (мс) или `null`. */
    sendAtTs?: number | null;
    /** Ф30-2: догон к сроку не уйдёт, если после подтверждения в беседе ответят. */
    cancelIfReply?: boolean;
    /** Ф27-1: `{id: имя}` для `<@U…>` текста (Slack) — в поле правки `@Имя`, при отправке обратно `<@id>`. */
    mentionNames?: Record<string, string>;
    /** Ф69-1: очередь держит стоп первых сообщений новым людям. */
    held?: 'peer_flood' | null;
  }>(),
  { held: null, paced: false, notBeforeTs: null, sendAtTs: null, cancelIfReply: false, mentionNames: undefined },
);

const timed = computed(() => props.sendAtTs !== null);
const slot = computed(() =>
  props.status === 'queued' && (props.paced || timed.value) && props.notBeforeTs !== null
    ? slotLabel(props.notBeforeTs)
    : null,
);
const sendAt = computed(() => (props.sendAtTs !== null ? sendAtLabel(props.sendAtTs) : null));
/** Ф30-2: « · не уйдёт, если ответят» к пометке времени догона с `cancel_if_reply`. */
const replyMark = computed(() => cancelIfReplyMark(props.sendAtTs, props.cancelIfReply));
/** Ф30-1: время отправки прошло (409 `send_at_passed`) — показать «Отправить сейчас». */
const passed = ref<string | null>(null);

let editMap: MentionMap = {};
function labelled(text: string): string {
  const labels = toLabels(text, props.mentionNames);
  editMap = labels.map;
  return labels.text;
}
const editText = ref(labelled(props.text));
const userTouchedEdit = ref(false);
const busy = ref(false);
const approveButton = ref<HTMLButtonElement | null>(null);
const keyLabel = submitKeyLabel();
const showHint = showSubmitHint(); // Ф11-4 И5б
const errorReason = ref<string | null>(null);
const actionError = ref<string | null>(null);

watch(
  () => props.text,
  (newText) => {
    if (!userTouchedEdit.value) editText.value = labelled(newText);
  },
);

function onEditInput(): void {
  userTouchedEdit.value = true;
}

watch(
  () => props.status,
  async (status) => {
    if (status === 'failed' || status === 'unknown') {
      try {
        const outbox = await api.getOutbox(props.outboxId);
        errorReason.value = outboxErrorText(outbox.error);
      } catch {
        errorReason.value = null;
      }
    } else {
      errorReason.value = null;
    }
  },
  { immediate: true },
);

function describeError(err: unknown): string {
  return err instanceof api.ApiError ? err.message : 'Не удалось выполнить действие. Проверьте соединение.';
}

async function approve(now = false): Promise<void> {
  if (editText.value.trim() === '') return;
  busy.value = true;
  actionError.value = null;
  passed.value = null;
  const text = toWire(editText.value, pruneMap(editText.value, editMap));
  try {
    if (now) await store.approveOutboxNow(props.outboxId, text);
    else await store.approveOutbox(props.outboxId, text);
  } catch (err) {
    if (isSendAtPassed(err)) passed.value = describeError(err);
    else actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}

async function cancel(): Promise<void> {
  busy.value = true;
  actionError.value = null;
  try {
    await store.cancelOutbox(props.outboxId);
  } catch (err) {
    actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}

/** 24.10: «Отправить сейчас» у подтверждённой строки по графику или к сроку. */
async function releaseNow(): Promise<void> {
  busy.value = true;
  actionError.value = null;
  try {
    await store.sendOutboxNow(props.outboxId);
  } catch (err) {
    actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}

async function resolve(decision: ResolveUnknownDecision): Promise<void> {
  busy.value = true;
  actionError.value = null;
  try {
    await store.resolveUnknown(props.outboxId, decision);
  } catch (err) {
    actionError.value = describeError(err);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="outbox-status"
    :class="`outbox-status--${status}`"
  >
    <template v-if="status === 'pending_approval'">
      <p class="outbox-label">
        <span class="outbox-num">черновик #{{ outboxId }}</span> · ждёт подтверждения<span v-if="paced"> · по графику</span><span
          v-else-if="sendAt !== null"
          class="outbox-send-at"
        > · {{ sendAt }}{{ replyMark }}</span>
      </p>
      <textarea
        v-model="editText"
        :disabled="busy"
        rows="2"
        enterkeyhint="enter"
        @input="onEditInput"
        @keydown="onSubmitKey($event, approveButton)"
      />
      <div class="outbox-actions">
        <button
          ref="approveButton"
          type="button"
          :title="keyLabel"
          :disabled="busy || editText.trim() === ''"
          @click="approve()"
        >
          Отправить
        </button>
        <span
          v-if="showHint"
          class="submit-hint"
          aria-hidden="true"
        >{{ keyLabel }}</span>
        <button
          type="button"
          :disabled="busy"
          @click="cancel"
        >
          Отменить
        </button>
      </div>
      <p
        v-if="passed !== null"
        class="outbox-label outbox-label--warn outbox-send-at-passed"
        role="alert"
      >
        {{ passed }}
        <button
          type="button"
          class="outbox-send-now"
          :disabled="busy || editText.trim() === ''"
          @click="approve(true)"
        >
          Отправить сейчас
        </button>
      </p>
    </template>
    <template v-else-if="status === 'queued' && slot !== null">
      <p class="outbox-label outbox-label--scheduled">
        <span class="outbox-num outbox-num--quiet">#{{ outboxId }}</span> · {{ timed ? `${slot}${replyMark}` : `${slot} (по графику)` }}<span
          v-if="held === 'peer_flood'"
          class="outbox-held"
        > · ждёт: стоп первых сообщений новым людям</span>
      </p>
      <div class="outbox-actions">
        <button
          type="button"
          class="outbox-release-now"
          :disabled="busy"
          title="Уйдёт сразу — без графика и срока"
          @click="releaseNow"
        >
          Отправить сейчас
        </button>
        <button
          type="button"
          :disabled="busy"
          @click="cancel"
        >
          Отменить
        </button>
      </div>
    </template>
    <p
      v-else-if="status === 'queued'"
      class="outbox-label"
    >
      <span class="outbox-num outbox-num--quiet">#{{ outboxId }}</span> · в очереди<span
        v-if="held === 'peer_flood'"
        class="outbox-held"
      > · ждёт: стоп первых сообщений новым людям</span>
    </p>
    <p
      v-else-if="status === 'sending'"
      class="outbox-label"
    >
      <span class="outbox-num outbox-num--quiet">#{{ outboxId }}</span> · отправляется
    </p>
    <template v-else-if="status === 'unknown'">
      <p class="outbox-label outbox-label--warn">
        <span class="outbox-num outbox-num--quiet">#{{ outboxId }}</span> · возможно отправлено — проверь
      </p>
      <div class="outbox-actions">
        <button
          type="button"
          :disabled="busy"
          @click="resolve('sent')"
        >
          Отправлено
        </button>
        <button
          type="button"
          :disabled="busy"
          @click="resolve('not_sent')"
        >
          Не отправлено
        </button>
        <button
          type="button"
          :disabled="busy"
          @click="resolve('retry')"
        >
          Повторить
        </button>
      </div>
    </template>
    <template v-else-if="status === 'failed'">
      <p class="outbox-label outbox-label--error">
        <span class="outbox-num outbox-num--quiet">#{{ outboxId }}</span> · не отправлено<span v-if="errorReason"> — {{ errorReason }}</span>
      </p>
    </template>
    <p
      v-else-if="status === 'cancelled'"
      class="outbox-label"
    >
      <span class="outbox-num outbox-num--quiet">#{{ outboxId }}</span> · отменено
    </p>
    <p
      v-else-if="status === 'sent'"
      class="outbox-label outbox-label--ok"
    >
      доставлено
    </p>
    <p
      v-if="actionError"
      class="outbox-action-error"
      role="alert"
    >
      {{ actionError }}
    </p>
  </div>
</template>
