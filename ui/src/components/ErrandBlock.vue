<script setup lang="ts">
// Ф66-3 (A): «у агента» на карточках, которые сами не несут поручения (просьбы по группам WhatsApp и «выйти»). Поручение
// и его возврат ядро кладёт в карточку очереди владельца с тем же `ref` (`group_op:<id>`, `leave:<id>`): строка хода
// «решает агент: …» с «Остановить агента», блок «Агент вернул тебе» (`ErrandReturned`). Кнопки самой карточки остаются
// на месте — их показывает родитель (ответ владельца закрывает поручение, Ф65-1). Тексты — чужой ввод: интерполяция.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import { store } from '../store/instance';
import { openWrite } from '../store/writeModal';
import { isShowcase } from '../util/showcase';
import { errandLine } from '../util/owner_channel';
import ErrandHands from './ErrandHands.vue';
import ErrandReturned from './ErrandReturned.vue';

const props = defineProps<{ itemRef: string; title: string; testPrefix: string }>();

const busy = ref(false);
const error = ref<string | null>(null);
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  timer = setInterval(() => {
    now.value = Date.now();
  }, 15000);
});
onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer);
});

const queueItem = computed(() => store.state.owner_channel.items.find((i) => i.ref === props.itemRef));
// Витрина: агента владельца нет — блок пуст.
const showcase = isShowcase();
const errand = computed(() => (showcase ? null : (queueItem.value?.errand ?? null)));
const errandText = computed(() => errandLine(errand.value, now.value));
const returned = computed(() => (showcase || errand.value !== null ? null : (queueItem.value?.errand_returned ?? null)));

async function takeBack(): Promise<void> {
  const open = errand.value;
  if (open === null) return;
  busy.value = true;
  error.value = null;
  try {
    const result = await store.cancelErrand(open.id);
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}

async function letDecide(text: string): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.startErrand({ ref: props.itemRef }, text === '' ? { action: 'solve' } : { text });
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    v-if="errandText || returned"
    class="errand-block"
  >
    <p
      v-if="errandText"
      class="errand-block-line"
      :data-test="`${testPrefix}-errand`"
    >
      {{ errandText }}
      <button
        type="button"
        class="link-button"
        :data-test="`${testPrefix}-take-back`"
        :disabled="busy"
        @click="takeBack"
      >
        Остановить агента
      </button>
    </p>
    <ErrandHands
      v-if="errandText && errand"
      :errand="errand"
      :test-prefix="testPrefix"
    />
    <ErrandReturned
      v-else-if="returned"
      :test-prefix="testPrefix"
      :returned="returned"
      :busy="busy"
      @say="letDecide"
      @talk="openWrite({ kind: 'owner_channel' }, { ref: itemRef, title })"
    />
    <p
      v-if="error"
      class="errand-block-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
