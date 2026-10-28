<script setup lang="ts">
// Запрос разрешения сессии целиком — окном поверх (только чтение; решают кнопки карточки). Вход инструмента и
// причина — недоверенный ввод: только текстом. Закрывается по ×, Esc и клику мимо.
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import type { PermissionInputView } from '../util/dispatch';

defineProps<{ title: string; view: PermissionInputView; reason: string | null; truncated: boolean }>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLElement | null>(null);

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
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay role-text-overlay"
      @click.self="emit('close')"
    >
      <div
        ref="dialog"
        class="channel-modal role-text-modal request-text-modal"
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
            v-if="view.description"
            class="work-request-description"
          >
            {{ view.description }}
          </p>
          <pre
            v-if="view.command !== null"
            class="role-text-content request-text-command"
          >{{ view.command }}</pre>
          <pre
            v-if="view.json !== null"
            class="role-text-content request-text-json"
          >{{ view.json }}</pre>
          <p
            v-if="truncated"
            class="work-request-truncated"
          >
            показано не всё
          </p>
          <p
            v-if="reason"
            class="work-request-reason"
          >
            причина: {{ reason }}
          </p>
        </div>
      </div>
    </div>
  </Teleport>
</template>
