<script setup lang="ts">
// Ф8-5 B1: просмотр картинки во весь экран поверх ленты. Закрытие — Esc или клик; в альбоме — листание
// стрелками (кнопки и клавиши ←/→). Картинка — тот же `/api/v1/media/{id}`, что превью (кэш браузера).
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

interface ViewerItem {
  src: string;
  alt: string;
}

const props = defineProps<{ items: ViewerItem[]; start: number }>();
const emit = defineEmits<{ close: [] }>();

const index = ref(Math.min(Math.max(props.start, 0), Math.max(props.items.length - 1, 0)));
const current = computed<ViewerItem | null>(() => props.items[index.value] ?? null);

function prev(): void {
  if (index.value > 0) index.value -= 1;
}

function next(): void {
  if (index.value < props.items.length - 1) index.value += 1;
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close');
  else if (event.key === 'ArrowLeft') prev();
  else if (event.key === 'ArrowRight') next();
}

onMounted(() => window.addEventListener('keydown', onKey));
onBeforeUnmount(() => window.removeEventListener('keydown', onKey));
</script>

<template>
  <div
    class="media-viewer"
    role="dialog"
    aria-modal="true"
    @click="emit('close')"
  >
    <img
      v-if="current"
      class="media-viewer__img"
      :src="current.src"
      :alt="current.alt"
    >
    <template v-if="items.length > 1">
      <button
        type="button"
        class="media-viewer__nav media-viewer__prev"
        :disabled="index === 0"
        aria-label="предыдущая"
        @click.stop="prev"
      >
        ‹
      </button>
      <button
        type="button"
        class="media-viewer__nav media-viewer__next"
        :disabled="index === items.length - 1"
        aria-label="следующая"
        @click.stop="next"
      >
        ›
      </button>
      <span class="media-viewer__counter">{{ index + 1 }} / {{ items.length }}</span>
    </template>
  </div>
</template>
