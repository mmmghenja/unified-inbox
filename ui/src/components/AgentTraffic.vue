<script setup lang="ts">
// Ф35-3: блок «Переписка агентов» — последние 50 сообщений агентов между собой, новые сверху; «ещё» догружает старые.
// Тексты и имена — чужой ввод: только текстовая интерполяция.
import { computed, ref } from 'vue';

import type { PeerMsg, PeerSide } from '../api/types';
import { store } from '../store/instance';
import { agentTraffic, batchLabel, sideName, statusLabel, unreadMinutes } from '../store/peerMsgs';

const props = defineProps<{ nowMs: number }>();

const PAGE = 50;
const shown = ref(PAGE);
const loading = ref(false);

const all = computed(() => agentTraffic(store.state.peerMsgs.items));
const rows = computed(() => all.value.slice(0, shown.value));
const canMore = computed(() => all.value.length > shown.value || store.state.peerMsgs.more);

function projectName(id: number): string {
  if (id === 0) return 'вне проектов';
  return store.state.projects.find((p) => p.id === id)?.name ?? `проект ${id}`;
}

function side(s: PeerSide | null): string {
  return s === null ? '?' : `${projectName(s.project_id)} / ${sideName(s)}`;
}

function time(ts: number): string {
  return new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function unread(msg: PeerMsg): number | null {
  return unreadMinutes(msg, props.nowMs);
}

async function more(): Promise<void> {
  if (loading.value) return;
  loading.value = true;
  try {
    if (all.value.length <= shown.value) {
      const ids = Object.keys(store.state.peerMsgs.items).map(Number);
      if (ids.length > 0) await store.loadPeerMsgs(Math.min(...ids));
    }
    shown.value += PAGE;
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <section
    class="agent-traffic"
    aria-label="Переписка агентов"
  >
    <h2 class="agents-title">
      Переписка агентов
    </h2>
    <p
      v-if="rows.length === 0"
      class="agents-empty"
    >
      Агенты между собой не переписывались
    </p>
    <ol class="traffic-list">
      <li
        v-for="msg in rows"
        :key="msg.id"
        class="traffic-row"
      >
        <div class="traffic-head">
          <span class="traffic-who">{{ side(msg.from) }} → {{ side(msg.to) }}</span>
          <span class="agent-meta">{{ time(msg.created_ts) }}</span>
          <span class="agent-meta traffic-status">{{ statusLabel(msg) }}</span>
          <span
            v-if="batchLabel(msg, props.nowMs)"
            class="agent-meta traffic-batch"
          >{{ batchLabel(msg, props.nowMs) }}</span>
          <span
            v-if="unread(msg) !== null"
            class="agent-meta traffic-warn"
          >не прочитано {{ unread(msg) }} мин</span>
        </div>
        <div class="traffic-text">
          {{ msg.text }}
        </div>
      </li>
    </ol>
    <button
      v-if="rows.length > 0 && canMore"
      type="button"
      class="link-button traffic-more"
      :disabled="loading"
      @click="more"
    >
      ещё
    </button>
  </section>
</template>
