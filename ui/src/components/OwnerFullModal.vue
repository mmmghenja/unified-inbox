<script setup lang="ts">
// Ф56-3: полный текст карточки очереди владельца — окном поверх (разовое чтение; решают кнопки карточки).
// Закрывается по ×, Esc и клику мимо.
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import type { OwnerItem } from '../api/types';
import OwnerFullBody from './OwnerFullBody.vue';

// `canOpenSession` — в шапке «Открыть сессию» (карточка из очереди: полный разговор с приславшей её сессией).
defineProps<{ title: string; item: OwnerItem; answerId?: string; canOpenSession?: boolean }>();
const emit = defineEmits<{ close: []; openSession: [] }>();

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
        class="channel-modal role-text-modal owner-full-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">{{ title }}</b>
          <span class="board-col-spacer" />
          <button
            v-if="canOpenSession"
            type="button"
            class="channel-modal-project owner-modal-session"
            data-test="owner-modal-open-session"
            @click="emit('openSession')"
          >
            Открыть сессию
          </button>
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
          <OwnerFullBody :item="item" />
        </div>
        <!-- Ф58-1 C: сюда карточка переносит свои элементы ответа (одно состояние, разметка не дублируется) -->
        <div
          v-if="answerId"
          :id="`owner-modal-answer-${answerId}`"
          class="owner-modal-answer"
        />
      </div>
    </div>
  </Teleport>
</template>
