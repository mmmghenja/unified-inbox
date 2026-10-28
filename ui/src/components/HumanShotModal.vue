<script setup lang="ts">
// Ф54-1: скриншот заявки «нужен человек» целиком — окном поверх (разовое чтение). Закрывается по ×, Esc и клику мимо.
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

defineProps<{ src: string; title: string }>();
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
        class="channel-modal role-text-modal human-shot-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Скриншот"
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
          <img
            class="human-shot-full"
            :src="src"
            alt="скриншот"
          >
        </div>
      </div>
    </div>
  </Teleport>
</template>
