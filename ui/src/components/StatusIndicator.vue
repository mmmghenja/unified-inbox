<script setup lang="ts">
// Ф10-6 (решение 6): индикатор связи — «● N с» зелёным; «лента не обновляется» / «синхронизация не удалась» —
// красным текстом, как прежде в верхней полосе. `compact` (телефон) — только точка (красная при беде), тап
// показывает текст. Давность — секунды с последнего кадра потока; часы тикают раз в секунду.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import { store } from '../store/instance';
import { usePopup } from '../util/popup';

const props = withDefaults(defineProps<{ compact?: boolean }>(), { compact: false });

const nowTick = ref(Date.now());
let tickTimer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  tickTimer = setInterval(() => {
    nowTick.value = Date.now();
  }, 1000);
});
onBeforeUnmount(() => {
  if (tickTimer !== null) clearInterval(tickTimer);
});

const secondsSinceLastFrame = computed(() => {
  const lastFrameAt = store.state.staleness.lastFrameAt;
  if (lastFrameAt === null) return null;
  return Math.max(0, Math.floor((nowTick.value - lastFrameAt) / 1000));
});

// Ф8-1 A6: неудавшийся `/sync` — без текста ошибки сервера (он в консоли).
const secondsSinceSyncError = computed(() => {
  const syncError = store.state.syncError;
  if (syncError === null) return null;
  return Math.max(0, Math.floor((nowTick.value - syncError.at) / 1000));
});

const bad = computed(() => store.state.staleness.stale || secondsSinceSyncError.value !== null);

const streamText = computed(() => {
  const seconds = secondsSinceLastFrame.value;
  if (seconds === null) return 'лента ещё не подключилась';
  if (store.state.staleness.stale) return `лента не обновляется (${seconds} с)`;
  return `${seconds} с`;
});

const syncErrorText = computed(() =>
  secondsSinceSyncError.value === null
    ? null
    : `синхронизация не удалась (${secondsSinceSyncError.value} с назад) — повторяю`,
);

const fullText = computed(() =>
  [
    secondsSinceLastFrame.value !== null && !store.state.staleness.stale
      ? `обновлено ${secondsSinceLastFrame.value} с назад`
      : streamText.value,
    syncErrorText.value,
  ]
    .filter((x) => x !== null)
    .join('; '),
);

const showText = ref(false);
const dotButton = ref<HTMLElement | null>(null);
const dotPopup = ref<HTMLElement | null>(null);
// Ф80-1 A2: подсказка у точки закрывается Esc и нажатием мимо (общий механизм `util/popup`).
usePopup({ open: showText, button: () => dotButton.value, panel: () => dotPopup.value });
</script>

<template>
  <span
    v-if="props.compact"
    class="status-indicator status-indicator--compact"
  >
    <button
      ref="dotButton"
      type="button"
      class="status-dot-button"
      :class="{ bad }"
      :aria-expanded="showText"
      :aria-label="`Связь: ${fullText}`"
      @click="showText = !showText"
    >
      <span class="status-dot" />
    </button>
    <span
      v-if="showText"
      ref="dotPopup"
      class="staleness status-indicator-popup"
      :class="{ stale: bad }"
      role="status"
    >{{ fullText }}</span>
  </span>
  <span
    v-else
    class="status-indicator"
  >
    <span
      class="staleness"
      :class="{ stale: store.state.staleness.stale, 'staleness--ok': !bad && secondsSinceLastFrame !== null }"
      role="status"
      :title="fullText"
    >
      <span
        v-if="!store.state.staleness.stale && secondsSinceLastFrame !== null"
        class="status-dot"
      />
      {{ streamText }}
    </span>
    <span
      v-if="syncErrorText !== null"
      class="sync-error"
      role="alert"
    >
      {{ syncErrorText }}
    </span>
  </span>
</template>
