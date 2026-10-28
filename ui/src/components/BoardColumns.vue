<script setup lang="ts">
// Ф41-6a (2.4): ряд столбцов доски. Видно `columns` штук (ширина не меньше 320 px), остальные — прокруткой вбок.
// Порядок — порядок `open` раскладки. Столбцы: `session` (окно сессии хаба), `agent` (внешняя сессия), `owner_channel`
// (окно «Агент» — очередь владельца). Столбец беседы появится в 6б. Повторное «открыть» уже открытого — прокрутка и подсветка.
import { computed, nextTick, watch } from 'vue';

import { boardState, FLASH_MS, itemKey } from '../store/board';
import { store } from '../store/instance';
import { buildAgentLines } from '../store/peerMsgs';
import BoardAgent from './BoardAgent.vue';
import BoardChannel from './BoardChannel.vue';
import BoardColumnShell from './BoardColumnShell.vue';
import BoardSession from './BoardSession.vue';
import AgentQueue from './AgentQueue.vue';

defineProps<{ nowMs?: number }>();

const layout = computed(() => boardState.layout);
const lines = computed(() => buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs));
const rowEl = computed(() => ({ '--board-n': String(layout.value.columns) }));

watch(
  () => boardState.focusSeq,
  async () => {
    const key = boardState.focusKey;
    if (key === null) return;
    await nextTick();
    const el = document.querySelector<HTMLElement>(`.board-columns [data-col-key="${key}"]`);
    if (el === null) return;
    el.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), FLASH_MS);
  },
);
</script>

<template>
  <div
    class="board-columns"
    :style="rowEl"
  >
    <p
      v-if="layout.open.length === 0"
      class="board-columns-empty"
    >
      Выбери слева сессию или беседу — она откроется здесь столбцом
    </p>
    <template
      v-for="item in layout.open"
      :key="itemKey(item)"
    >
      <BoardSession
        v-if="item.kind === 'session' && item.id !== undefined && store.state.sessions.items[item.id] !== undefined"
        :item="item"
        :session="store.state.sessions.items[item.id]!"
      />
      <BoardAgent
        v-else-if="item.kind === 'agent'"
        :item="item"
        :line="lines.find((l) => l.key === item.key) ?? null"
        :offline="boardState.offline.find((o) => o.key === item.key) ?? null"
        :now-ms="nowMs"
      />
      <BoardChannel
        v-else-if="item.kind === 'channel' && item.id !== undefined"
        :item="item"
        :channel-id="item.id"
      />
      <BoardColumnShell
        v-else-if="item.kind === 'owner_channel'"
        :item="item"
        project="все проекты"
        title="Агент"
      >
        <AgentQueue :now-ms="nowMs" />
      </BoardColumnShell>
      <BoardColumnShell
        v-else
        :item="item"
        :title="item.kind === 'session' ? 'сессия' : 'беседа'"
      >
        <p class="board-empty">
          Сессия закрыта или ещё не загрузилась.
        </p>
      </BoardColumnShell>
    </template>
  </div>
</template>
