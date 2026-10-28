<script setup lang="ts">
// Ф10-6 (решение 6): счётчики очередей outbox — только когда > 0. Поведение кликов прежнее: «ждёт: N» ведёт
// на черновики (на телефоне — вкладка «Ждёт», там черновики со всех проектов), «неизвестно: N» открывает
// панель-список, `dead_letter` — простой счётчик с подсказкой (списка в API нет).
import { computed, ref } from 'vue';

import { store } from '../store/instance';
import { usePopup } from '../util/popup';
import OutboxQueuePanel from './OutboxQueuePanel.vue';

// Ф20-4 A3: `part` делит счётчики на тревожные (`alerts`: «неизвестно», `dead_letter` — в полосе сами) и
// спокойные (`calm`: «ждёт: N» — в меню «⋯»); по умолчанию все.
const props = withDefaults(defineProps<{ phone?: boolean; part?: 'all' | 'alerts' | 'calm' }>(), {
  phone: false,
  part: 'all',
});

const approval = computed(() => store.state.status?.outbox_approval ?? 0);
const unknown = computed(() => store.state.status?.outbox_unknown ?? 0);
const deadLetterTotal = computed(() => {
  const dl = store.state.status?.dead_letter ?? {};
  return Object.values(dl).reduce((sum, n) => sum + n, 0);
});

const unknownOpen = ref(false);
const anchorEl = ref<HTMLElement | null>(null);
// Ф80-1 A2: общий механизм меню (`util/popup`); якорь включает кнопку и панель.
usePopup({ open: unknownOpen, panel: () => anchorEl.value, place: false });

function openDrafts(): void {
  unknownOpen.value = false;
  if (props.phone) store.setPhoneTab('wait');
  else store.openHome('drafts');
}
</script>

<template>
  <span class="queue-counters">
    <!-- Ф8-6 C4: «ждёт: N» ведёт на раздел «Черновики» главной (там адрес, «ответ на» и действия). -->
    <button
      v-if="approval > 0 && part !== 'alerts'"
      type="button"
      class="queue-counter queue-counter--drafts"
      @click="openDrafts"
    >
      ждёт: {{ approval }}
    </button>

    <span
      v-if="part !== 'calm' && (unknown > 0 || unknownOpen)"
      ref="anchorEl"
      class="queue-anchor"
    >
      <button
        type="button"
        class="queue-counter queue-counter--unknown"
        :aria-pressed="unknownOpen"
        @click="unknownOpen = !unknownOpen"
      >
        неизвестно: {{ unknown }}
      </button>
      <OutboxQueuePanel
        v-if="unknownOpen"
        status="unknown"
        @close="unknownOpen = false"
      />
    </span>

    <span
      v-if="part !== 'calm' && deadLetterTotal > 0"
      class="queue-counter queue-counter--dead"
      title="В API нет списка dead_letter — разбор через `hub status` (вне UI)"
    >
      dead_letter: {{ deadLetterTotal }}
    </span>
  </span>
</template>
