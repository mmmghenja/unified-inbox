<script setup lang="ts">
// Ф48-2 D: «Сжатие…» из «⋯» сессии — окно поверх: флажок «Сжимать автоматически» и порог в тысячах токенов (пусто —
// умолчание 200). Сохраняются только изменённые ключи (`PATCH /sessions/{id}`); отказ ядра показывается текстом.
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import type { HubSession } from '../api/types';
import { store } from '../store/instance';
import { buildCompactPatch, compactFieldText, compactHint } from '../util/sessionCompact';

const props = defineProps<{ session: HubSession }>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLElement | null>(null);
const auto = ref(props.session.compact_auto ?? true);
const field = ref(compactFieldText(props.session));
const busy = ref(false);
const error = ref<string | null>(null);

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  event.stopPropagation();
  emit('close');
}
onMounted(() => {
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => dialog.value?.focus());
});
onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown));

async function save(): Promise<void> {
  const built = buildCompactPatch(props.session, auto.value, field.value);
  if (!built.ok) {
    error.value = built.error;
    return;
  }
  if (Object.keys(built.body).length === 0) {
    emit('close');
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    const result = await store.setSessionCompact(props.session.id, built.body);
    if (result.ok) emit('close');
    else error.value = result.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay session-compact-overlay"
      @click.self="emit('close')"
    >
      <form
        ref="dialog"
        class="channel-modal session-compact-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Сжатие"
        tabindex="-1"
        @submit.prevent="save"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">Сжатие</b>
          <span class="board-col-spacer" />
          <button
            type="button"
            class="channel-modal-close"
            aria-label="Закрыть"
            @click="emit('close')"
          >
            ×
          </button>
        </header>
        <div class="session-compact-body">
          <label class="session-compact-auto">
            <input
              v-model="auto"
              type="checkbox"
              class="session-compact-auto-input"
            >
            Сжимать автоматически
          </label>
          <label class="work-field">
            <span>Порог, тыс. токенов</span>
            <input
              v-model="field"
              type="text"
              inputmode="numeric"
              class="session-compact-input"
              placeholder="200"
              autocomplete="off"
            >
          </label>
          <p class="session-compact-hint">
            {{ compactHint(session) }}
          </p>
          <div class="work-actions">
            <button
              type="submit"
              class="session-compact-save"
              :disabled="busy"
            >
              Сохранить
            </button>
            <button
              type="button"
              class="link-button"
              @click="emit('close')"
            >
              отмена
            </button>
          </div>
          <p
            v-if="error"
            class="work-error"
            role="alert"
          >
            {{ error }}
          </p>
        </div>
      </form>
    </div>
  </Teleport>
</template>
