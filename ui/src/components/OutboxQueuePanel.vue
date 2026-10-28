<script setup lang="ts">
// F8 (приёмка 6b): панель-список для кликабельных счётчиков «ждёт подтверждения» / «неизвестный результат»
// в TopBar. Использует `GET /outbox?status=&limit=`, добавленную архитектором во время приёмки. Перезапрос —
// на каждое изменение `sync.lastSeq` (то есть после каждой применённой пачки `/sync`), пока панель открыта.
// `channel_title` и `text` — недоверенный ввод стороннего источника, рендерятся ТОЛЬКО текстом (обычная
// text-интерполяция Vue, без вставки сырого HTML — как и везде в проекте).
import { computed, onMounted, ref, watch } from 'vue';

import * as api from '../api/client';
import type { OutboxListItem, OutboxQueueStatus } from '../api/types';
import { store } from '../store/instance';
import { filterByNumber } from '../util/draftNumber';
import { showMentions } from '../util/mentions';
import NumberSearch from './NumberSearch.vue';

const props = defineProps<{ status: OutboxQueueStatus }>();
const emit = defineEmits<{ close: [] }>();

const items = ref<OutboxListItem[]>([]);
const numberText = ref('');
const shownItems = computed(() => filterByNumber(items.value, numberText.value));
const loading = ref(false);
const loadError = ref<string | null>(null);

async function load(): Promise<void> {
  loading.value = true;
  try {
    items.value = await api.listOutbox(props.status);
    loadError.value = null;
  } catch (err) {
    loadError.value = err instanceof api.ApiError ? err.message : 'Не удалось загрузить список.';
  } finally {
    loading.value = false;
  }
}

onMounted(load);

// Пока панель открыта (смонтирована) — перезапрос после каждой применённой пачки /sync.
watch(() => store.state.sync.lastSeq, () => void load());

function openItem(item: OutboxListItem): void {
  void store.openOutboxQueueItem(item);
  emit('close');
}

const TITLE: Record<OutboxQueueStatus, string> = {
  pending_approval: 'Ждёт подтверждения',
  unknown: 'Неизвестный результат',
  failed: 'Не отправлено',
};
</script>

<template>
  <div
    class="queue-panel"
    role="dialog"
    :aria-label="TITLE[status]"
  >
    <div class="queue-panel-header">
      <strong>{{ TITLE[status] }}</strong>
      <button
        type="button"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </div>
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
      Пусто.
    </p>
    <NumberSearch
      v-if="items.length > 0"
      v-model="numberText"
    />
    <p
      v-if="items.length > 0 && shownItems.length === 0"
      class="empty-hint number-search-empty"
    >
      Нет номера «{{ numberText }}».
    </p>
    <ul v-if="items.length > 0">
      <li
        v-for="item in shownItems"
        :key="item.id"
      >
        <button
          type="button"
          class="queue-item"
          @click="openItem(item)"
        >
          <span class="draft-card-num draft-card-num--quiet">#{{ item.id }}</span>
          <span class="queue-item-channel">{{ item.channel_title ?? '(без названия)' }}</span>
          <span
            v-if="item.origin === 'api'"
            class="queue-item-origin"
          >от агента</span>
          <span class="queue-item-text">{{ showMentions(item.text ?? '', item.mention_names) }}</span>
        </button>
      </li>
    </ul>
  </div>
</template>
