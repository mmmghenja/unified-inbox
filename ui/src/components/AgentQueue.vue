<script setup lang="ts">
// Ф56-3: окно «Агент» (`docs/architecture.md`): всё, что у сессий хаба и проектов ждёт владельца, —
// одна очередь (`GET /owner_channel/queue`). Шапка: состояние агента, счётчик, фильтр «все · в канале · только мне»;
// ниже карточки по порядку (срочное → «нужен человек» → разрешения и вопросы → остальное, старые выше).
// Живёт и столбцом доски, и вкладкой телефона. Пока смонтировано — очередь перечитывается раз в минуту и по `/sync`.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import { store } from '../store/instance';
import { openTerminal } from '../store/terminal';
import { openWrite } from '../store/writeModal';
import { successorOf, withHeld, ownerHold } from '../store/ownerHold';
import { isShowcase } from '../util/showcase';
import { filterOwner, sortOwner, type OwnerFilter } from '../util/owner_channel';
import OwnerCard from './OwnerCard.vue';
import OwnerResults from './OwnerResults.vue';

const props = defineProps<{ nowMs?: number }>();
// Витрина: «Написать…» ведёт в разговор «Мой агент» — его нет.
const showcase = isShowcase();

const filter = ref<OwnerFilter>('all');
const tick = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | null = null;
let stopWatch: (() => void) | null = null;

const state = computed(() => store.state.owner_channel);
const now = computed(() => props.nowMs ?? tick.value);
const online = computed(() => state.value.agent.online);
// Карточка, с которой владелец работает (окно, «развернуть», набранный ответ), остаётся и после ухода из очереди —
// с плашкой «Есть новее» (`store/ownerHold.ts`).
const visible = computed(() => {
  const shown = withHeld(state.value.items, ownerHold.cards);
  const gone = new Set(shown.filter((c) => c.gone).map((c) => c.item.ref));
  return sortOwner(filterOwner(shown.map((c) => c.item), filter.value), now.value).map((item) => ({
    item,
    gone: gone.has(item.ref),
    successor: gone.has(item.ref) ? successorOf(item, state.value.items) : null,
  }));
});

const FILTERS: { key: OwnerFilter; label: string }[] = [
  { key: 'all', label: 'все' },
  { key: 'channel', label: 'в канале' },
  { key: 'mine', label: 'только мне' },
];

onMounted(() => {
  stopWatch = store.watchOwner();
  ticker = setInterval(() => {
    tick.value = Date.now();
  }, 15_000); // Ф58-3: «решает агент … N мин» — раз в 15 с
});
onBeforeUnmount(() => {
  stopWatch?.();
  if (ticker !== null) clearInterval(ticker);
});
</script>

<template>
  <section
    class="agent-queue"
    aria-label="Агент"
  >
    <header class="agent-queue-head">
      <span
        class="agent-state"
        :class="online ? 'agent-state--on' : 'agent-state--off'"
        data-test="agent-state"
      >
        <span class="agent-dot" />
        {{ online ? 'агент на связи' : 'агент не на связи' }}
      </span>
      <span
        class="agent-count"
        data-test="agent-count"
      >{{ state.items.length }}</span>
      <button
        v-if="!showcase"
        type="button"
        class="agent-filter agent-write"
        data-test="agent-write"
        @click="openWrite({ kind: 'owner_channel' })"
      >
        Написать…<span
          v-if="(state.agent.talk_unread ?? 0) > 0"
          class="badge badge-react"
        >{{ state.agent.talk_unread }}</span>
      </button>
      <button
        type="button"
        class="agent-filter agent-terminal"
        title="Терминал снизу"
        data-test="agent-terminal"
        @click="openTerminal()"
      >
        ⌨ Терминал
      </button>
      <span class="agent-filters">
        <button
          v-for="f in FILTERS"
          :key="f.key"
          type="button"
          class="agent-filter"
          :aria-pressed="filter === f.key"
          :data-filter="f.key"
          @click="filter = f.key"
        >
          {{ f.label }}
        </button>
      </span>
    </header>
    <p
      v-if="state.notice"
      class="agent-notice"
      role="status"
      data-test="agent-notice"
    >
      {{ state.notice }}
      <button
        type="button"
        class="link-button"
        @click="state.notice = null"
      >
        ×
      </button>
    </p>
    <p
      v-if="state.error"
      class="home-error"
      role="alert"
    >
      {{ state.error }}
    </p>
    <div class="agent-queue-list">
      <p
        v-if="state.loaded && visible.length === 0"
        class="board-empty agent-empty"
        data-test="agent-empty"
      >
        Ничего не ждёт
      </p>
      <OwnerCard
        v-for="card in visible"
        :key="card.item.ref"
        :item="card.item"
        :gone="card.gone"
        :successor="card.successor"
        :now-ms="now"
        :agent-online="online"
      />
      <OwnerResults />
    </div>
  </section>
</template>
