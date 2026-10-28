<script setup lang="ts">
// Ф47-1: окно одной сессии проекта в видах «По проектам» и «Один проект» — те же `BoardSession` / `BoardAgent`, что
// у своего набора, но столбец не свой (`fixed`): без «закрыть» и «подвинуть». Сессии хаба, которой больше нет
// (закрыта), показывается заглушка.
import { computed } from 'vue';

import { boardState, type ColumnItem } from '../store/board';
import { store } from '../store/instance';
import { buildAgentLines } from '../store/peerMsgs';
import BoardAgent from './BoardAgent.vue';
import BoardColumnShell from './BoardColumnShell.vue';
import BoardSession from './BoardSession.vue';

const props = defineProps<{ item: ColumnItem; nowMs?: number }>();

const lines = computed(() => buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs));
const session = computed(() => (props.item.kind === 'session' && props.item.id !== undefined ? (store.state.sessions.items[props.item.id] ?? null) : null));
</script>

<template>
  <BoardSession
    v-if="item.kind === 'session' && session !== null"
    :item="item"
    :session="session"
    fixed
  />
  <BoardAgent
    v-else-if="item.kind === 'agent'"
    :item="item"
    :line="lines.find((l) => l.key === item.key) ?? null"
    :offline="boardState.offline.find((o) => o.key === item.key) ?? null"
    :now-ms="nowMs"
    fixed
  />
  <BoardColumnShell
    v-else
    :item="item"
    title="сессия"
    fixed
  >
    <p class="board-empty">
      Сессия закрыта или ещё не загрузилась.
    </p>
  </BoardColumnShell>
</template>
