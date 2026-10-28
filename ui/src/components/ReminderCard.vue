<script setup lang="ts">
// Ф34-2 (`docs/architecture.md`: сработавшее напоминание — строка «Нужна реакция» столбца
// проекта и карточка «Ждёт» телефона: ⏰, название беседы, заметка, кто поставил; «Открыть беседу», «Готово»,
// «Отложить ▾» (на час / до завтра 10:00 / своё время). Заметку пишет сессия, название — собеседник: только
// текстовая интерполяция.
import { computed, ref } from 'vue';

import type { Reminder } from '../api/types';
import { store } from '../store/instance';
import {
  fromLocalInput,
  inAnHour,
  isoOf,
  isRecurring,
  lastDoneLabel,
  toLocalInput,
  tomorrowTen,
  whenLabel,
  whoLabel,
} from '../util/reminders';

const props = defineProps<{ reminder: Reminder }>();

const busy = ref(false);
const error = ref<string | null>(null);
const snoozeOpen = ref(false);
const custom = ref('');

const hasChannel = computed(() => props.reminder.channel_id !== null);
const recurring = computed(() => isRecurring(props.reminder));
const missed = computed(() => props.reminder.missed ?? 0);
const lastDone = computed(() => lastDoneLabel(props.reminder));
const confirmStop = ref(false);
const title = computed(() => {
  if (!hasChannel.value) return `Напоминание ${props.reminder.role?.trim() || 'сессии'}`;
  return props.reminder.channel?.title?.trim() || `беседа #${props.reminder.channel_id}`;
});
// «сработало в …» — по fired_ts у повторяемого (due_ts там уже следующий срок); у разового срок был due_ts.
const firedLabel = computed(() => whenLabel(props.reminder.fired_ts ?? props.reminder.due_ts));

function openChannel(): void {
  if (props.reminder.channel_id === null) return;
  const projectId = props.reminder.channel?.project_id ?? props.reminder.project_id;
  void store.openChannelAt(projectId !== null && projectId > 0 ? projectId : null, props.reminder.channel_id);
}

async function run(fields: { at?: string; close?: boolean; stop?: boolean }): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.patchReminder(props.reminder, fields);
    if (result === 'stale') error.value = 'Напоминание уже изменилось — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуйте ещё раз.';
    else {
      snoozeOpen.value = false;
      confirmStop.value = false;
    }
  } finally {
    busy.value = false;
  }
}

function snooze(ts: number): Promise<void> {
  return run({ at: isoOf(ts) });
}

function toggleSnooze(): void {
  snoozeOpen.value = !snoozeOpen.value;
  if (snoozeOpen.value && custom.value === '') custom.value = toLocalInput(inAnHour());
}

function snoozeCustom(): Promise<void> {
  const ts = fromLocalInput(custom.value);
  if (ts === null) {
    error.value = 'Укажите дату и время.';
    return Promise.resolve();
  }
  return snooze(ts);
}
</script>

<template>
  <div
    class="reminder-card"
    :data-reminder-id="reminder.id"
  >
    <p class="reminder-head">
      <span aria-hidden="true">⏰</span>
      <strong class="reminder-title">{{ title }}</strong>
      <span class="reminder-no">#{{ reminder.id }}</span>
      <span
        v-if="recurring"
        class="reminder-every"
      >{{ reminder.every_text }}</span>
      <span
        v-if="recurring && missed > 0"
        class="badge-react reminder-missed"
      >пропущено {{ missed }}</span>
      <span class="reminder-meta">{{ recurring ? 'сработало в' : 'срок был' }} {{ firedLabel }} · поставил: {{ whoLabel(reminder) }}</span>
    </p>
    <p
      v-if="recurring && lastDone"
      class="reminder-meta reminder-last-done"
    >
      {{ lastDone }}
    </p>
    <p
      v-if="reminder.note"
      class="reminder-note"
    >
      {{ reminder.note }}
    </p>
    <div class="reminder-actions">
      <button
        v-if="hasChannel"
        type="button"
        class="reminder-open"
        @click="openChannel"
      >
        Открыть беседу
      </button>
      <button
        type="button"
        class="reminder-done"
        :disabled="busy"
        @click="run({ close: true })"
      >
        {{ recurring ? 'Сделано' : 'Готово' }}
      </button>
      <button
        v-if="recurring && !confirmStop"
        type="button"
        class="reminder-stop"
        :disabled="busy"
        @click="confirmStop = true"
      >
        Снять
      </button>
      <template v-if="recurring && confirmStop">
        <button
          type="button"
          class="reminder-stop-confirm"
          :disabled="busy"
          @click="run({ stop: true })"
        >
          Снять насовсем?
        </button>
        <button
          type="button"
          class="reminder-stop-cancel"
          @click="confirmStop = false"
        >
          Нет
        </button>
      </template>
      <button
        type="button"
        class="reminder-snooze"
        :aria-expanded="snoozeOpen"
        :disabled="busy"
        @click="toggleSnooze"
      >
        Отложить ▾
      </button>
    </div>
    <div
      v-if="snoozeOpen"
      class="reminder-snooze-menu"
    >
      <button
        type="button"
        class="reminder-snooze-hour"
        :disabled="busy"
        @click="snooze(inAnHour())"
      >
        на час
      </button>
      <button
        type="button"
        class="reminder-snooze-tomorrow"
        :disabled="busy"
        @click="snooze(tomorrowTen())"
      >
        до завтра 10:00
      </button>
      <span class="reminder-custom">
        <input
          v-model="custom"
          type="datetime-local"
          class="reminder-custom-input"
          aria-label="Своё время"
        >
        <button
          type="button"
          class="reminder-snooze-custom"
          :disabled="busy"
          @click="snoozeCustom"
        >
          Отложить
        </button>
      </span>
    </div>
    <p
      v-if="error"
      class="reminder-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
