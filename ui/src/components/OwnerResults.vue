<script setup lang="ts">
// Ф65-1 (И4): итоги агента — закрытые карточки, по которым он оставил итог (`errand_result` из `GET /owner_channel/closed`).
// Свёрнутый блок под очередью; читается по раскрытию. Итог — текст агента (маскирован ядром): только интерполяцией.
import { computed, onMounted, ref } from 'vue';

import * as api from '../api/client';
import type { OwnerClosedRow } from '../api/types';
import { basisLabel } from '../util/agentPatterns';
import { decidedByLabel, kindLabel } from '../util/owner_channel';

const rows = ref<OwnerClosedRow[]>([]);
const loaded = ref(false);
const failed = ref(false);
const showAll = ref(false);
const RECENT_MS = 2 * 3600_000;
const RECENT_N = 3;

// Ф92-1 (A2): последние 3 строки за 2 часа видны сразу (блок раскрыт), остальное — по «ещё N».
const recent = computed(() => {
  const from = Date.now() - RECENT_MS;
  return rows.value.filter((r) => r.closed_ts >= from).slice(0, RECENT_N);
});
const shown = computed(() => (recent.value.length > 0 && !showAll.value ? recent.value : rows.value));
const hiddenCount = computed(() => (recent.value.length > 0 && !showAll.value ? rows.value.length - recent.value.length : 0));
const opened = ref(false);

function replyLabel(r: OwnerClosedRow): string | null {
  const a = r.agent_reply;
  if (!a) return null;
  if (a.action === 'reply') return a.text ? `ответил сессии: ${a.text}` : 'ответил сессии';
  return a.action === 'deny' ? 'отклонил без ответа' : 'принял без ответа';
}

function clock(ts: number): string {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

async function load(): Promise<void> {
  failed.value = false;
  try {
    const res = await api.getOwnerClosed(50);
    // Итог агента или «сразу да» (ядро: `payload.by = 'auto'`): остальное закрытое здесь не показываем.
    rows.value = res.items.filter((r) => (typeof r.errand_result === 'string' && r.errand_result !== '') || r.payload?.by === 'auto' || (typeof r.basis === 'string' && r.basis !== '') || r.agent_reply != null);
    loaded.value = true;
    if (recent.value.length > 0) opened.value = true;
  } catch {
    failed.value = true;
  }
}

async function onToggle(event: Event): Promise<void> {
  opened.value = (event.target as HTMLDetailsElement).open;
  if (opened.value) await load();
}

onMounted(load);
</script>

<template>
  <details
    class="owner-results"
    data-test="owner-results"
    :open="opened"
    @toggle="onToggle"
  >
    <summary>Итоги агента и «сразу да»</summary>
    <p
      v-if="failed"
      class="work-error"
      role="alert"
    >
      Не получилось прочитать — попробуй ещё раз.
    </p>
    <p
      v-else-if="loaded && rows.length === 0"
      class="board-empty"
    >
      Пока ничего нет.
    </p>
    <ul
      v-else
      class="owner-results-list"
    >
      <li
        v-for="r in shown"
        :key="r.id"
        class="owner-result"
        data-test="owner-result"
      >
        <span class="owner-result-meta">{{ kindLabel(r.kind) }} · {{ clock(r.closed_ts) }}<template v-if="decidedByLabel(r)"> · решил: {{ decidedByLabel(r) }}</template></span>
        <span
          v-if="r.basis"
          class="owner-result-basis"
          data-test="owner-result-basis"
        >{{ basisLabel(r.basis) }}</span>
        <span
          v-if="replyLabel(r)"
          class="owner-result-text"
          data-test="owner-result-reply"
        >{{ replyLabel(r) }}</span>
        <span
          v-if="r.errand_result"
          class="owner-result-text"
        >{{ r.errand_result }}</span>
      </li>
    </ul>
    <button
      v-if="hiddenCount > 0"
      type="button"
      class="link-button"
      data-test="owner-results-more"
      @click="showAll = true"
    >
      ещё {{ hiddenCount }}
    </button>
  </details>
</template>
