<script setup lang="ts">
// Ф34-2 (решение 6): панель напоминаний беседы под шапкой — ждущие (заметка, срок, «Снять», «Перенести») и форма
// «Напомнить…» (время + заметка + «снять, если собеседник ответит», по умолчанию включено). Заметка — чужие
// данные (пишет сессия): только текстовая интерполяция. Ошибки ядра (422/429/409) показываются текстом `detail`.
import { computed, ref } from 'vue';

import { ApiError } from '../api/client';
import type { Reminder } from '../api/types';
import { store } from '../store/instance';
import { usePanel } from '../util/popup';
import {
  EVERY_CHOICES,
  everyOf,
  fromLocalInput,
  inAnHour,
  isoOf,
  isRecurring,
  lastDoneLabel,
  pendingOfChannel,
  toLocalInput,
  tomorrowTen,
  whenLabel,
  whoLabel,
} from '../util/reminders';

const props = defineProps<{ channelId: number; archived: boolean }>();
const emit = defineEmits<{ close: [] }>();
// Ф80-1 A2: встроенная форма — Esc закрывает, открытие другого меню (например «Без подтверждения») тоже; нажатие мимо
// не закрывает (в ней заполняют поля).
const root = ref<HTMLElement | null>(null);
usePanel(() => root.value, () => emit('close'), { keepOnOutside: true });

const pending = computed(() => pendingOfChannel(Object.values(store.state.reminders.items), props.channelId));

const at = ref(toLocalInput(inAnHour()));
const note = ref('');
const cancelIfReply = ref(true);
const everyChoice = ref('');
const everyHours = ref('4');
const busy = ref(false);
const error = ref<string | null>(null);
const moving = ref<number | null>(null);
const moveAt = ref('');

function fail(err: unknown): void {
  if (err instanceof ApiError && err.message) error.value = err.message;
  else error.value = 'Не отправилось — попробуйте ещё раз.';
}

async function create(): Promise<void> {
  const ts = fromLocalInput(at.value);
  if (ts === null) {
    error.value = 'Укажите дату и время.';
    return;
  }
  const every = everyOf(everyChoice.value, everyHours.value);
  if (every === undefined) {
    error.value = 'Часы — целое число от 1.';
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    await store.createReminder({
      channel_id: props.channelId,
      at: isoOf(ts),
      ...(note.value.trim() !== '' ? { note: note.value.trim() } : {}),
      ...(every !== null ? { every } : { cancel_if_reply: cancelIfReply.value }),
    });
    note.value = '';
    everyChoice.value = '';
    at.value = toLocalInput(inAnHour());
  } catch (err) {
    fail(err);
  } finally {
    busy.value = false;
  }
}

async function unset(reminder: Reminder, fields: { close?: boolean; stop?: boolean } = { close: true }): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.patchReminder(reminder, fields);
    if (result === 'stale') error.value = 'Напоминание уже изменилось — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуйте ещё раз.';
  } finally {
    busy.value = false;
  }
}

function startMove(reminder: Reminder): void {
  moving.value = moving.value === reminder.id ? null : reminder.id;
  moveAt.value = toLocalInput(reminder.due_ts);
}

async function move(reminder: Reminder): Promise<void> {
  const ts = fromLocalInput(moveAt.value);
  if (ts === null) {
    error.value = 'Укажите дату и время.';
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    const result = await store.patchReminder(reminder, { at: isoOf(ts) });
    if (result === 'stale') error.value = 'Напоминание уже изменилось — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуйте ещё раз.';
    else moving.value = null;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    ref="root"
    class="channel-reminders"
    role="region"
    aria-label="Напоминания беседы"
  >
    <ul
      v-if="pending.length > 0"
      class="channel-reminders-list"
    >
      <li
        v-for="reminder in pending"
        :key="reminder.id"
        class="channel-reminder"
        :data-reminder-id="reminder.id"
      >
        <span class="reminder-no">#{{ reminder.id }}</span>
        <span class="channel-reminder-when">⏰ {{ whenLabel(reminder.due_ts) }}</span>
        <span
          v-if="isRecurring(reminder)"
          class="channel-reminder-every"
        >{{ reminder.every_text }}</span>
        <span
          v-if="isRecurring(reminder) && (reminder.missed ?? 0) > 0"
          class="badge-react reminder-missed"
        >пропущено {{ reminder.missed }}</span>
        <span
          v-if="isRecurring(reminder) && lastDoneLabel(reminder)"
          class="channel-reminder-last"
        >{{ lastDoneLabel(reminder) }}</span>
        <span class="channel-reminder-who">{{ whoLabel(reminder) }}</span>
        <span
          v-if="reminder.note"
          class="channel-reminder-note"
        >{{ reminder.note }}</span>
        <span class="channel-reminder-actions">
          <button
            v-if="isRecurring(reminder) && reminder.status === 'fired'"
            type="button"
            class="reminder-done"
            :disabled="busy"
            @click="unset(reminder, { close: true })"
          >Сделано</button>
          <button
            type="button"
            class="reminder-unset"
            :disabled="busy"
            @click="isRecurring(reminder) ? unset(reminder, { stop: true }) : unset(reminder)"
          >Снять</button>
          <button
            type="button"
            class="reminder-move"
            :disabled="busy"
            @click="startMove(reminder)"
          >Перенести</button>
        </span>
        <span
          v-if="moving === reminder.id"
          class="channel-reminder-move"
        >
          <input
            v-model="moveAt"
            type="datetime-local"
            class="reminder-move-input"
            aria-label="Новое время"
          >
          <button
            type="button"
            class="reminder-move-ok"
            :disabled="busy"
            @click="move(reminder)"
          >Перенести</button>
        </span>
      </li>
    </ul>
    <p
      v-else
      class="channel-reminders-empty"
    >
      Напоминаний нет.
    </p>

    <form
      v-if="!archived"
      class="channel-reminder-form"
      @submit.prevent="create"
    >
      <strong>Напомнить…</strong>
      <input
        v-model="at"
        type="datetime-local"
        class="reminder-at"
        aria-label="Когда напомнить"
      >
      <button
        type="button"
        class="reminder-preset"
        @click="at = toLocalInput(inAnHour())"
      >
        через час
      </button>
      <button
        type="button"
        class="reminder-preset"
        @click="at = toLocalInput(tomorrowTen())"
      >
        завтра 10:00
      </button>
      <input
        v-model="note"
        type="text"
        class="reminder-note-input"
        maxlength="1000"
        placeholder="Заметка (необязательно)"
        aria-label="Заметка"
      >
      <select
        v-model="everyChoice"
        class="reminder-every-select"
        aria-label="Период"
      >
        <option
          v-for="choice in EVERY_CHOICES"
          :key="choice.value"
          :value="choice.value"
        >
          {{ choice.label }}
        </option>
      </select>
      <input
        v-if="everyChoice === 'hours'"
        v-model="everyHours"
        type="number"
        min="1"
        class="reminder-every-hours"
        aria-label="Часов между напоминаниями"
      >
      <label
        v-if="everyChoice === ''"
        class="reminder-cancel-if-reply"
      >
        <input
          v-model="cancelIfReply"
          type="checkbox"
        >
        снять, если собеседник ответит
      </label>
      <button
        type="submit"
        class="reminder-create"
        :disabled="busy"
      >
        Поставить
      </button>
    </form>
    <p
      v-if="error"
      class="reminder-error"
      role="alert"
    >
      {{ error }}
    </p>
    <button
      type="button"
      class="link-button reminder-close"
      @click="emit('close')"
    >
      закрыть
    </button>
  </div>
</template>
