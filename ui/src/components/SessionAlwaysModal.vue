<script setup lang="ts">
// Ф99-7.2: «Разрешения…» из «⋯» сессии — окно поверх со списком «Всегда в этой сессии» (инструмент MCP или каталог
// записи вне проекта) и «×» у каждого. Ставится «всегда» только кнопкой на карточке разрешения; передачей дел не
// переносится. Отказ ядра — текстом.
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import type { HubSession } from '../api/types';
import { store } from '../store/instance';
import { alwaysLabel } from '../util/sessionAlways';

const props = defineProps<{ session: HubSession }>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLElement | null>(null);
const busy = ref<string | null>(null);
const error = ref<string | null>(null);
const items = computed(() => props.session.always ?? []);

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

async function remove(key: string): Promise<void> {
  busy.value = key;
  error.value = null;
  try {
    const result = await store.removeSessionAlways(props.session.id, key);
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = null;
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay session-always-overlay"
      @click.self="emit('close')"
    >
      <div
        ref="dialog"
        class="channel-modal session-always-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Разрешения"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">Разрешения</b>
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
        <div class="session-always-body">
          <p class="session-always-hint">
            «Всегда в этой сессии» — без карточки, в любом режиме. Ставится кнопкой на карточке разрешения.
          </p>
          <p
            v-if="items.length === 0"
            class="session-always-empty"
          >
            Пусто.
          </p>
          <ul
            v-else
            class="session-always-list"
          >
            <li
              v-for="item in items"
              :key="item.key"
              class="session-always-item"
              :data-key="item.key"
            >
              <span class="session-always-label">{{ alwaysLabel(item.key) }}</span>
              <button
                type="button"
                class="session-always-remove link-button"
                :aria-label="`Снять: ${alwaysLabel(item.key)}`"
                :disabled="busy !== null"
                @click="remove(item.key)"
              >
                ×
              </button>
            </li>
          </ul>
          <p
            v-if="error"
            class="work-error"
            role="alert"
          >
            {{ error }}
          </p>
        </div>
      </div>
    </div>
  </Teleport>
</template>
