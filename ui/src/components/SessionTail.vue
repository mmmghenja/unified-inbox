<script setup lang="ts">
// Ф99-7.3 (П12): лента внешней сессии Claude Code «только смотреть» — последние события её стенограммы в форме
// ленты сессии хаба (`WorkEventFeed`), без поля ввода (писать ей можно только как агенту — `AgentDialog`).
// Сессия на машине ядра обновляется раз в 5 с, сессия другой машины — раз в минуту (пульс MCP). Не Claude Code —
// строка вместо ленты.
import { onBeforeUnmount, onMounted, ref } from 'vue';

import { getSubTranscript } from '../api/client';
import type { SubTranscript } from '../api/types';
import WorkEventFeed from './WorkEventFeed.vue';

const props = defineProps<{ subId: number }>();

const LOCAL_POLL_MS = 5000;
const data = ref<SubTranscript | null>(null);
const failed = ref(false);
let timer: ReturnType<typeof setTimeout> | null = null;
let stopped = false;

async function refresh(): Promise<void> {
  try {
    data.value = await getSubTranscript(props.subId);
    failed.value = false;
  } catch {
    failed.value = true;
  }
  if (stopped || data.value?.supported === false) return;
  const wait = data.value?.refresh_s != null ? data.value.refresh_s * 1000 : LOCAL_POLL_MS;
  timer = setTimeout(() => void refresh(), wait);
}

onMounted(() => void refresh());
onBeforeUnmount(() => {
  stopped = true;
  if (timer !== null) clearTimeout(timer);
});
</script>

<template>
  <section
    class="session-tail"
    aria-label="Лента сессии"
  >
    <p
      v-if="data !== null && !data.supported"
      class="session-tail-note"
    >
      Лента доступна только для Claude Code.
    </p>
    <template v-else-if="data !== null">
      <p
        v-if="data.remote"
        class="session-tail-note"
      >
        Лента только для чтения · обновляется раз в минуту.
      </p>
      <WorkEventFeed
        :events="data.events"
        :older-done="true"
      />
    </template>
    <p
      v-else-if="failed"
      class="session-tail-note"
    >
      Лента не загрузилась.
    </p>
  </section>
</template>

<style scoped>
.session-tail {
  max-height: 40vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}
.session-tail-note {
  margin: 0.25rem 0;
  font-size: 0.85em;
  opacity: 0.7;
}
</style>
