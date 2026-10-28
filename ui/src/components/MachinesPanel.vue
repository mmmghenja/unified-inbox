<script setup lang="ts">
// Ф77-1 D3: «⋯ → Машины» — окно поверх (панель как «Люди»): таблица реестра машин. Имя правится на месте: клик →
// поле, Enter сохраняет (`PATCH /machines/{host}`), Esc отменяет, пустое имя снимает его; ошибки 404/422 — текстом
// под полем. Версия, отличная от версии машины ядра, — жёлтым. Имена и hostname — чужой ввод: только текстом.
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import * as api from '../api/client';
import type { Machine } from '../api/types';
import { agoText, routeWord, versionDiffers } from '../util/machines';
import { usePanel } from '../util/popup';

const emit = defineEmits<{ close: [] }>();
// Ф80-1 B1: Esc и нажатие мимо закрывают панель (общий механизм `util/popup`).
const panelRoot = ref<HTMLElement | null>(null);
usePanel(() => panelRoot.value, () => emit('close'));

const items = ref<Machine[]>([]);
const coreVersion = ref<string | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);
const editing = ref<string | null>(null);
const draft = ref('');
const rowError = ref<{ host: string; text: string } | null>(null);
const busy = ref(false);
const input = ref<HTMLInputElement[] | HTMLInputElement | null>(null);
const nowMs = ref(Date.now());
let tick: ReturnType<typeof setInterval> | null = null;

function errorText(err: unknown): string {
  if (err instanceof api.ApiError) {
    if (err.status === 404) return 'Нет такой машины.';
    if (err.status === 422) return err.message && err.message !== '' ? err.message : 'Имя: 1–32 печатных символа.';
    if (err.status === 403) return 'Имя меняется только из интерфейса хаба на ноутбуке или телефоне.';
    return err.message;
  }
  if (err instanceof api.NetworkError) return 'Нет связи с хабом — попробуй ещё раз.';
  return 'Не удалось — попробуй ещё раз.';
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    const result = await api.listMachines();
    items.value = result.items;
    coreVersion.value = result.items[0]?.version ?? null;
  } catch (err) {
    error.value = errorText(err);
  } finally {
    loading.value = false;
  }
}

async function startEdit(m: Machine): Promise<void> {
  if (m.host === null) return;
  editing.value = m.host;
  draft.value = m.name ?? '';
  rowError.value = null;
  await nextTick();
  const el = Array.isArray(input.value) ? input.value[0] : input.value;
  el?.focus();
}

function cancel(): void {
  editing.value = null;
  rowError.value = null;
}

async function save(m: Machine): Promise<void> {
  if (m.host === null || busy.value) return;
  busy.value = true;
  rowError.value = null;
  try {
    const result = await api.renameMachine(m.host, draft.value.trim());
    items.value = items.value.map((x) => (x.host === m.host ? result.machine : x));
    editing.value = null;
  } catch (err) {
    rowError.value = { host: m.host, text: errorText(err) };
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  void load();
  tick = setInterval(() => (nowMs.value = Date.now()), 30_000);
});
onBeforeUnmount(() => {
  if (tick !== null) clearInterval(tick);
});
</script>

<template>
  <div
    ref="panelRoot"
    class="access-panel machines-panel"
    role="dialog"
    aria-label="Машины"
  >
    <div class="access-panel-header">
      <strong>Машины</strong>
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
      class="machines-meta"
    >
      Загрузка…
    </p>
    <div
      v-else
      class="machines-table"
    >
      <div
        v-for="m in items"
        :key="m.host ?? 'core'"
        class="machines-row"
        data-test="machine-row"
      >
        <template v-if="editing !== null && editing === m.host">
          <input
            ref="input"
            v-model="draft"
            class="machines-name-edit"
            maxlength="32"
            placeholder="Имя (пусто — снять)"
            aria-label="Имя машины"
            data-test="machine-name-input"
            :disabled="busy"
            @keydown.enter.prevent="save(m)"
            @keydown.esc.prevent.stop="cancel"
          >
        </template>
        <button
          v-else
          type="button"
          class="link-button machines-name"
          data-test="machine-name"
          :disabled="m.host === null"
          :title="m.host === null ? undefined : 'Переименовать'"
          @click="startEdit(m)"
        >
          {{ m.display }}
        </button>
        <!-- Ф80-1 F5 (Д16): hostname — только когда он отличается от имени (иначе одна машина выглядит двумя строками). -->
        <span
          v-if="m.hostname && m.hostname !== m.display"
          class="machines-meta"
        >{{ m.hostname }}</span>
        <span
          v-if="m.core"
          class="machines-meta"
        >машина ядра</span>
        <span
          v-else-if="routeWord(m.route)"
          class="machines-meta"
        >{{ routeWord(m.route) }}</span>
        <span
          v-if="m.version"
          class="machines-meta"
          :class="{ 'machines-version--diff': versionDiffers(m, coreVersion) }"
          :title="versionDiffers(m, coreVersion) ? `Отличается от версии машины ядра (${coreVersion})` : undefined"
          data-test="machine-version"
        >{{ m.version }}</span>
        <span
          v-if="m.last_ts !== null && !m.core"
          class="machines-meta"
        >{{ agoText(m.last_ts, nowMs) }}</span>
        <span
          v-if="!m.online"
          class="machines-offline"
          data-test="machine-offline"
        >не на связи</span>
        <p
          v-if="rowError !== null && rowError.host === m.host"
          class="machines-error"
          role="alert"
          data-test="machine-error"
        >
          {{ rowError.text }}
        </p>
      </div>
      <p
        v-if="items.length === 0 && error === null"
        class="machines-meta"
      >
        Машин пока нет.
      </p>
    </div>
  </div>
</template>
