<script setup lang="ts">
// Ф99-6: расшифровка голосового целиком — окном поверх (разовое чтение). Текст — чужой ввод: только интерполяцией.
// Закрывается ×, Esc и кликом мимо.
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

defineProps<{ title: string; text: string }>();
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
        class="channel-modal role-text-modal transcript-modal"
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
            class="transcript__text"
            data-test="transcript-modal-text"
            style="white-space: pre-wrap"
          >
            {{ text }}
          </p>
        </div>
      </div>
    </div>
  </Teleport>
</template>
