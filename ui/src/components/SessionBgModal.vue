<script setup lang="ts">
// Ф48-1: окно «Агенты сессии <роль>» (разовое чтение — окном поверх): строка на задачу — название, тип, состояние,
// чем занят, последний инструмент, шагов, токенов, сколько идёт (живой счётчик), у законченной — итог (по клику
// раскрывается). Задачи подагента — с отступом. Всё — чужой ввод: только текстом. Закрывается по ✕, Esc, клику мимо.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue';

import type { HubSession } from '../api/types';
import { bgElapsed, bgKindLabel, bgRows, bgStateLabel, bgTokens } from '../util/sessionBg';

const props = defineProps<{ session: HubSession; title: string }>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLElement | null>(null);
const now = ref(Date.now());
const expanded = reactive<Record<string, boolean>>({});
const rows = computed(() => bgRows(props.session.bg?.items ?? []));
let timer: ReturnType<typeof setInterval> | null = null;

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  event.stopPropagation();
  emit('close');
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown);
  timer = setInterval(() => (now.value = Date.now()), 1000);
  void nextTick(() => dialog.value?.focus());
});
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  if (timer !== null) clearInterval(timer);
});
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay session-bg-overlay"
      @click.self="emit('close')"
    >
      <div
        ref="dialog"
        class="channel-modal role-text-modal session-bg-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">{{ title }}</b>
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
        <div class="role-text-body">
          <p
            v-if="rows.length === 0"
            class="board-empty"
          >
            Задач нет
          </p>
          <ul
            v-else
            class="session-bg-list"
          >
            <li
              v-for="row in rows"
              :key="row.item.id"
              class="session-bg-item"
              :class="[`session-bg-item--${row.item.state}`, { 'session-bg-item--child': row.depth > 0 }]"
              :data-bg-id="row.item.id"
            >
              <div class="session-bg-main">
                <b class="session-bg-title">{{ row.item.title }}</b>
                <span class="session-bg-kind">{{ bgKindLabel(row.item) }}</span>
                <span class="session-bg-state">{{ bgStateLabel(row.item.state) }}</span>
                <span class="session-bg-time">{{ bgElapsed(row.item, now) }}</span>
              </div>
              <p
                v-if="row.item.kind === 'agent' && row.item.doing"
                class="session-bg-doing"
              >
                {{ row.item.doing }}
              </p>
              <p
                v-if="row.item.last_tool || row.item.tool_uses !== null || row.item.tokens !== null"
                class="session-bg-meta"
              >
                <span v-if="row.item.last_tool">{{ row.item.last_tool }}</span>
                <span v-if="row.item.tool_uses !== null">шагов: {{ row.item.tool_uses }}</span>
                <span v-if="row.item.tokens !== null">токенов: {{ bgTokens(row.item.tokens) }}</span>
              </p>
              <button
                v-if="row.item.state !== 'running' && row.item.note"
                type="button"
                class="session-bg-note"
                :class="{ 'session-bg-note--open': expanded[row.item.id] }"
                :aria-expanded="expanded[row.item.id] === true"
                @click="expanded[row.item.id] = !expanded[row.item.id]"
              >
                {{ row.item.note }}
              </button>
            </li>
          </ul>
        </div>
      </div>
    </div>
  </Teleport>
</template>
