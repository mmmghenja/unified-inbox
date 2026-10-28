<script setup lang="ts">
// Ф12-2 C (`docs/architecture.md`): подтверждение черновиков пачкой над списком черновиков
// (главная, столбец проекта, «Ждёт» на телефоне). «Выбрать все» — все черновики списка; «Подтвердить выбранные
// (N)» — `approve-batch` в порядке выбора флажками (в нём же раздаются слоты графика). Итог — строкой
// «подтверждено N, запланировано на ЧЧ:ММ–ЧЧ:ММ» и ошибки по пунктам. Текстов черновиков здесь нет.
import { computed, ref } from 'vue';

import * as api from '../api/client';
import type { OutboxListItem } from '../api/types';
import { store } from '../store/instance';
import { batchErrors, batchPassed, batchSummary } from '../util/pace';

/** Предел ядра на один запрос (`APPROVE_BATCH_MAX`). */
const BATCH_MAX = 50;

const props = defineProps<{ items: OutboxListItem[] }>();

const busy = ref(false);
const summary = ref<string | null>(null);
const errors = ref<string[]>([]);
// Ф30-1 C/П8: у черновиков, чьё время `send_at` прошло, — «Отправить сейчас» по одному.
const passed = ref<number[]>([]);

// Ф14-3 П10: черновик с файлами пачкой не подтверждается — «Выбрать все» его не берёт.
const ids = computed(() => props.items.filter((item) => (item.files ?? []).length === 0).map((item) => item.id));
const selected = computed(() => store.selectedDraftsOf(ids.value));
const allSelected = computed(() => ids.value.length > 0 && selected.value.length === ids.value.length);

function selectAll(): void {
  store.selectDrafts(ids.value);
}

async function approveSelected(): Promise<void> {
  const chosen = selected.value.slice(0, BATCH_MAX);
  if (chosen.length === 0 || busy.value) return;
  busy.value = true;
  summary.value = null;
  errors.value = [];
  passed.value = [];
  try {
    const results = await store.approveOutboxBatch(chosen);
    summary.value = batchSummary(results);
    errors.value = batchErrors(results);
    passed.value = batchPassed(results);
  } catch (err) {
    errors.value = [err instanceof api.ApiError ? err.message : 'Не удалось подтвердить. Проверьте соединение.'];
  } finally {
    busy.value = false;
  }
}

async function sendNow(outboxId: number): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await store.approveOutboxNow(outboxId);
    passed.value = passed.value.filter((id) => id !== outboxId);
    errors.value = errors.value.filter((line) => !line.startsWith(`#${outboxId}:`));
  } catch (err) {
    errors.value = [
      ...errors.value,
      `#${outboxId}: ${err instanceof api.ApiError ? err.message : 'Не удалось отправить. Проверьте соединение.'}`,
    ];
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    v-if="items.length > 0 || summary !== null || errors.length > 0 || passed.length > 0"
    class="draft-batch-bar"
  >
    <template v-if="items.length > 0">
      <button
        type="button"
        class="link-button draft-select-all"
        :disabled="busy || allSelected"
        @click="selectAll"
      >
        Выбрать все
      </button>
      <button
        type="button"
        class="draft-approve-selected"
        :disabled="busy || selected.length === 0"
        @click="approveSelected"
      >
        Подтвердить выбранные ({{ selected.length }})
      </button>
    </template>
    <p
      v-if="summary !== null"
      class="draft-batch-result"
      role="status"
    >
      {{ summary }}
    </p>
    <ul
      v-if="errors.length > 0"
      class="draft-batch-errors"
      role="alert"
    >
      <li
        v-for="line in errors"
        :key="line"
      >
        {{ line }}
      </li>
    </ul>
    <p
      v-for="id in passed"
      :key="`now-${id}`"
      class="draft-batch-send-now"
    >
      <button
        type="button"
        class="draft-send-now"
        :disabled="busy"
        @click="sendNow(id)"
      >
        Отправить сейчас #{{ id }}
      </button>
    </p>
  </div>
</template>
