<script setup lang="ts">
// Ф41-3б: строка «пишет…» в конце ленты окна сессии — текст блока, пока модель его набирает. Текст — через
// `ModelText` (безопасный markdown); перерисовка не чаще 4 раз в секунду. Размышления (`thinking`) — свёрнутый
// блок «думает…», раскрывается по клику. Рисуется `<li>` — слот хвоста списка событий.
import { onBeforeUnmount, ref, watch } from 'vue';

import type { SessionLive } from '../api/types';
import ModelText from './ModelText.vue';

const props = defineProps<{ live: SessionLive }>();

const LIVE_RENDER_MIN_MS = 250;

const shown = ref(props.live.text);
const open = ref(false);
let lastAt = Date.now();
let timer: ReturnType<typeof setTimeout> | null = null;

watch(
  () => props.live.text,
  (text) => {
    const wait = LIVE_RENDER_MIN_MS - (Date.now() - lastAt);
    if (wait <= 0) {
      lastAt = Date.now();
      shown.value = text;
      return;
    }
    if (timer !== null) return; // уже взведён — возьмёт самый свежий текст
    timer = setTimeout(() => {
      timer = null;
      lastAt = Date.now();
      shown.value = props.live.text;
    }, wait);
  },
);

// Новый блок — текст сразу, без ожидания.
watch(
  () => props.live.block,
  () => {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    lastAt = Date.now();
    shown.value = props.live.text;
    open.value = false;
  },
);

onBeforeUnmount(() => {
  if (timer !== null) clearTimeout(timer);
});
</script>

<template>
  <li
    class="work-event session-live"
    :class="`session-live--${live.kind}`"
    data-live="1"
  >
    <template v-if="live.kind === 'thinking'">
      <button
        type="button"
        class="link-button session-live-think"
        :aria-expanded="open"
        @click="open = !open"
      >
        {{ open ? '▾' : '▸' }} думает…
      </button>
      <ModelText
        v-if="open"
        :text="shown"
      />
    </template>
    <template v-else>
      <span class="session-live-mark">пишет…</span>
      <span
        v-if="live.cut"
        class="session-live-cut"
      >начало скрыто</span>
      <ModelText :text="shown" />
    </template>
  </li>
</template>
