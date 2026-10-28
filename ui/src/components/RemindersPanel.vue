<script setup lang="ts">
// Ф89-1 (`docs/architecture.md`): «⋯ → Напоминания» — все открытые напоминания по проектам
// (`GET /reminders?counts=1`): роль, беседа (клик — окном поверх) или «без беседы», заметка, период, ближайший срок,
// «пропущено K», «последний раз», «новых с прошлого раза». «Сделано», «Снять» (повторяемое), «Отложить».
// Заметка и название беседы — чужие данные: только текстовая интерполяция.
import { computed, onMounted, ref } from 'vue';

import * as api from '../api/client';
import type { Reminder } from '../api/types';
import { store } from '../store/instance';
import { isPhone } from '../util/layout';
import { usePanel } from '../util/popup';
import { inAnHour, isoOf, isRecurring, lastDoneLabel, tomorrowTen, whenLabel, whoLabel } from '../util/reminders';

const emit = defineEmits<{ close: [] }>();
const panelRoot = ref<HTMLElement | null>(null);
usePanel(() => panelRoot.value, () => emit('close'));

const items = ref<Reminder[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);
const busy = ref(false);
const snoozeFor = ref<number | null>(null);
const stopFor = ref<number | null>(null);

function projectName(projectId: number | null): string {
  if (projectId === null || projectId <= 0) return 'Неразобранное';
  return store.state.projects.find((p) => p.id === projectId)?.name ?? `проект #${projectId}`;
}

const groups = computed(() => {
  const byProject = new Map<number, Reminder[]>();
  for (const r of items.value) {
    const key = r.project_id ?? 0;
    byProject.set(key, [...(byProject.get(key) ?? []), r]);
  }
  return [...byProject.entries()].map(([projectId, rows]) => ({ projectId, name: projectName(projectId || null), rows }));
});

async function load(): Promise<void> {
  error.value = null;
  try {
    const body = await api.listReminders({ counts: true, limit: 200 });
    items.value = Array.isArray(body?.items) ? body.items : [];
  } catch {
    error.value = 'Напоминания не загрузились — попробуй ещё раз.';
  } finally {
    loading.value = false;
  }
}

onMounted(() => void load());

function chanTitle(r: Reminder): string {
  return r.channel?.title?.trim() || `беседа #${r.channel_id}`;
}

function openChannel(r: Reminder): void {
  if (r.channel_id === null) return;
  if (isPhone()) void store.openChannelAt(r.channel?.project_id ?? r.project_id, r.channel_id);
  else void store.openChannelOverlay(r.channel_id);
}

async function act(r: Reminder, fields: api.ReminderPatch): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.patchReminder(r, fields);
    if (result === 'stale') error.value = 'Напоминание уже изменилось — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуй ещё раз.';
    snoozeFor.value = null;
    stopFor.value = null;
    await load();
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    ref="panelRoot"
    class="access-panel reminders-panel"
    role="dialog"
    aria-label="Напоминания"
  >
    <div class="access-panel-header">
      <strong>Напоминания</strong>
      <button
        type="button"
        class="access-panel-close"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </div>
    <p
      v-if="error !== null"
      class="access-error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="loading"
      class="reminders-meta"
    >
      Загрузка…
    </p>
    <p
      v-else-if="items.length === 0 && error === null"
      class="reminders-meta reminders-empty"
    >
      Открытых напоминаний нет
    </p>
    <section
      v-for="group in groups"
      :key="group.projectId"
      class="reminders-group"
      data-test="reminders-group"
    >
      <h4 class="reminders-group-title">
        {{ group.name }}
      </h4>
      <div
        v-for="r in group.rows"
        :key="r.id"
        class="reminders-row"
        data-test="reminders-row"
        :data-reminder-id="r.id"
      >
        <span class="reminder-no">#{{ r.id }}</span>
        <span class="reminders-role">{{ whoLabel(r) }}</span>
        <button
          v-if="r.channel_id !== null"
          type="button"
          class="link-button reminders-channel"
          @click="openChannel(r)"
        >
          {{ chanTitle(r) }}
        </button>
        <span
          v-else
          class="reminders-meta reminders-nochannel"
        >без беседы</span>
        <span class="reminders-every">{{ isRecurring(r) ? r.every_text : 'разово' }}</span>
        <span class="reminders-due">⏰ {{ whenLabel(r.due_ts) }}</span>
        <span
          v-if="isRecurring(r) && (r.missed ?? 0) > 0"
          class="badge-react reminder-missed"
        >пропущено {{ r.missed }}</span>
        <span
          v-if="isRecurring(r) && lastDoneLabel(r)"
          class="reminders-meta reminders-last"
        >{{ lastDoneLabel(r) }}</span>
        <span
          v-if="typeof r.new_since_done === 'number'"
          class="reminders-meta reminders-new"
        >новых с прошлого раза: {{ r.new_since_done }}</span>
        <p
          v-if="r.note"
          class="reminders-note"
        >
          {{ r.note }}
        </p>
        <span class="reminders-actions">
          <button
            v-if="!isRecurring(r) || r.status === 'fired'"
            type="button"
            class="reminder-done"
            :disabled="busy"
            @click="act(r, { close: true })"
          >Сделано</button>
          <button
            v-if="isRecurring(r) && stopFor !== r.id"
            type="button"
            class="reminder-stop"
            :disabled="busy"
            @click="stopFor = r.id"
          >Снять</button>
          <button
            v-if="isRecurring(r) && stopFor === r.id"
            type="button"
            class="reminder-stop-confirm"
            :disabled="busy"
            @click="act(r, { stop: true })"
          >Снять насовсем?</button>
          <button
            type="button"
            class="reminder-snooze"
            :disabled="busy"
            @click="snoozeFor = snoozeFor === r.id ? null : r.id"
          >Отложить ▾</button>
        </span>
        <span
          v-if="snoozeFor === r.id"
          class="reminders-actions"
        >
          <button
            type="button"
            class="reminder-snooze-hour"
            :disabled="busy"
            @click="act(r, { at: isoOf(inAnHour()) })"
          >на час</button>
          <button
            type="button"
            class="reminder-snooze-tomorrow"
            :disabled="busy"
            @click="act(r, { at: isoOf(tomorrowTen()) })"
          >до завтра 10:00</button>
        </span>
      </div>
    </section>
  </div>
</template>
