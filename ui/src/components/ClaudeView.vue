<script setup lang="ts">
// Ф20-4 B → Ф78-1: страница «⋯ → Агенты» (бывшая «Диспетчерская»; десктоп — страница под верхней полосой, телефон —
// страница без своей вкладки). Задач по «Го» больше нет: остались список агентов (`AgentsBlock`) и переписка
// агентов (`AgentTraffic`). Данные — из стора, обновление раз в 30 с, пока страница открыта.
import { onBeforeUnmount, onMounted, ref } from 'vue';

import { HOME_POLL_MS } from '../store/store';
import { store } from '../store/instance';
import AgentsBlock from './AgentsBlock.vue';
import AgentTraffic from './AgentTraffic.vue';

const props = withDefaults(defineProps<{ phone?: boolean }>(), { phone: false });

const nowTick = ref(Date.now());
let pollTimer: ReturnType<typeof setInterval> | null = null;
let tickTimer: ReturnType<typeof setInterval> | null = null;

function load(): void {
  void store.loadHome();
  void store.refreshAgentSubs(); // Ф36-2 B1: общий ограничитель с наблюдателем — один запрос, а не два
  void store.loadPeerMsgs();
}

// Ф36-2 B1: список агентов обновляется сам (таймер, возврат страницы, сигнал /sync), пока страница смонтирована.
let stopAgentWatch: (() => void) | null = null;

onMounted(() => {
  load();
  stopAgentWatch = store.watchAgentSubs();
  pollTimer = setInterval(load, HOME_POLL_MS);
  tickTimer = setInterval(() => {
    nowTick.value = Date.now();
  }, 30_000);
});

onBeforeUnmount(() => {
  stopAgentWatch?.();
  if (pollTimer !== null) clearInterval(pollTimer);
  if (tickTimer !== null) clearInterval(tickTimer);
});
</script>

<template>
  <main
    class="claude-view"
    :class="{ 'claude-view--phone': props.phone }"
    aria-label="Агенты"
  >
    <AgentsBlock :now-ms="nowTick" />
    <AgentTraffic :now-ms="nowTick" />
  </main>
</template>
