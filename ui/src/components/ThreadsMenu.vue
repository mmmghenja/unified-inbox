<script setup lang="ts">
// Спека 2e, раздел «Лента канала»: кнопка «Треды» в шапке канала (видна при `unread_threads > 0`, с числом)
// — выпадающий список подписанных тредов с непрочитанным (`GET /channels/{id}/threads?unread=1`). Закрывает
// «непрочитанное есть, а в ленте его не видно» (подписанный тред без родителя в открытой странице ленты).
// Тот же приём панели-списка, что `OutboxQueuePanel.vue` (`queue-anchor`/`queue-panel`).
import { ref, watch } from 'vue';

import type { ThreadListItem } from '../api/types';
import { store } from '../store/instance';
import { usePopup } from '../util/popup';

const props = defineProps<{ channelId: number }>();

const open = ref(false);
const btnEl = ref<HTMLElement | null>(null);
const panelEl = ref<HTMLElement | null>(null);
// Ф80-1 A2/A3: общий механизм меню; панель прижимается к краю экрана (Т4).
usePopup({ open, button: () => btnEl.value, panel: () => panelEl.value });
const items = ref<ThreadListItem[]>([]);
const loading = ref(false);
const loadError = ref<string | null>(null);

async function load(): Promise<void> {
  loading.value = true;
  try {
    items.value = await store.listThreads(props.channelId, { unread: true });
    loadError.value = null;
  } catch {
    loadError.value = 'Не удалось загрузить список.';
  } finally {
    loading.value = false;
  }
}

function toggle(): void {
  open.value = !open.value;
  if (open.value) void load();
}

function openItem(item: ThreadListItem): void {
  void store.openThread(props.channelId, item.thread_ext_id);
  open.value = false;
}

// Смена канала (или уход с него) — список показывал бы чужой канал, если оставить открытым.
watch(
  () => props.channelId,
  () => {
    open.value = false;
  },
);

function parentPreview(item: ThreadListItem): string {
  if (item.parent === null) return '(сообщение не загружено)';
  if (item.parent.hidden) return '(удалено у себя)'; // Ф8-4 П6
  return (item.parent.text ?? '(без текста)').slice(0, 80);
}
</script>

<template>
  <div class="queue-anchor">
    <button
      ref="btnEl"
      type="button"
      class="threads-button"
      :aria-pressed="open"
      @click="toggle"
    >
      Треды: {{ store.state.sync.channelsById[channelId]?.unread_threads ?? 0 }}
    </button>
    <div
      v-if="open"
      ref="panelEl"
      class="queue-panel threads-panel"
      role="dialog"
      aria-label="Треды"
    >
      <p v-if="loading && items.length === 0">
        Загрузка…
      </p>
      <p
        v-else-if="loadError"
        role="alert"
      >
        {{ loadError }}
      </p>
      <p v-else-if="items.length === 0">
        Нет непрочитанных тредов.
      </p>
      <ul v-else>
        <li
          v-for="item in items"
          :key="item.thread_ext_id"
        >
          <button
            type="button"
            class="queue-item thread-item"
            @click="openItem(item)"
          >
            <span class="queue-item-text">{{ parentPreview(item) }}</span>
            <span class="badge">{{ item.thread.unread }}</span>
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>
