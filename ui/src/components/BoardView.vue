<script setup lang="ts">
// Ф41-6a (`docs/architecture.md`): «Главная» на компьютере (`full`/`half`) = «Ждёт тебя»
// (`WaitBar` — панель поверх доски по кнопке «Ждёт тебя · N» верхней полосы) + полоса `BoardToolbar` + ряд [список | столбцы]. Страница не
// прокручивается; прокрутка — внутри панели «Ждёт», списка и каждого столбца. Здесь же держатся опросы данных, чтобы
// список жил и без открытых окон: главная, работа, сессии, агенты.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { boardState, loadLayout, loadOfflineLines, pruneSessions } from '../store/board';
import { HOME_POLL_MS } from '../store/store';
import { store } from '../store/instance';
import BoardColumns from './BoardColumns.vue';
import BoardOneProject from './BoardOneProject.vue';
import BoardProjects from './BoardProjects.vue';
import BoardList from './BoardList.vue';
import BoardToolbar from './BoardToolbar.vue';
import WaitBar from './WaitBar.vue';

const nowTick = ref(Date.now());
let pollTimer: ReturnType<typeof setInterval> | null = null;
let tickTimer: ReturnType<typeof setInterval> | null = null;
let stopSessions: (() => void) | null = null;
let stopAgents: (() => void) | null = null;

function load(): void {
  void store.loadHome();
  void store.loadJoins();
  void store.loadRuleRequests();
  void store.loadAgentRequests();
  void store.loadPresses();
  void store.loadHumanRequests();
  void store.loadLeaves();
  void store.loadGroupOps();
  void store.loadPeerMsgs();
  void loadOfflineLines();
}

onMounted(() => {
  void loadLayout();
  load();
  stopSessions = store.watchSessions();
  stopAgents = store.watchAgentSubs();
  void store.refreshAgentSubs(); // сразу, а не через полминуты: новая сессия видна, как только открыл
  pollTimer = setInterval(load, HOME_POLL_MS);
  tickTimer = setInterval(() => {
    nowTick.value = Date.now();
  }, 30_000);
});

onBeforeUnmount(() => {
  if (!store.state.channelModalOverlay) store.closeChannelModal();
  stopSessions?.();
  stopAgents?.();
  if (pollTimer !== null) clearInterval(pollTimer);
  if (tickTimer !== null) clearInterval(tickTimer);
});

// Столбец сессии, которой больше нет (закрыта), из раскладки убирается после загрузки списка сессий.
watch(
  () => store.state.sessions.loadedAt,
  (at) => {
    if (at !== null) pruneSessions(new Set(Object.keys(store.state.sessions.items).map(Number)));
  },
);

const layout = computed(() => boardState.layout);
</script>

<template>
  <main
    class="board-view"
    aria-label="Главная"
  >
    <WaitBar />
    <BoardToolbar />
    <div
      class="board-body"
      :class="{ 'board-body--collapsed': layout.list_collapsed }"
    >
      <BoardList :now-ms="nowTick" />
      <!-- Ф47-1: вид «Главной»; свой набор столбцов при смене вида не трогается. -->
      <BoardProjects
        v-if="layout.view === 'projects'"
        :now-ms="nowTick"
      />
      <BoardOneProject
        v-else-if="layout.view === 'project'"
        :now-ms="nowTick"
      />
      <BoardColumns
        v-else
        :now-ms="nowTick"
      />
    </div>
  </main>
</template>
