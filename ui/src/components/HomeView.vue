<script setup lang="ts">
// Ф10-6 A2/A4 (`docs/architecture.md`): главная — доска, столбец на проект (`ProjectColumn.vue`, порядок
// проектов из API — стабильный) и узкий последний столбец «Неразобранное», когда там что-то ждёт. Каждый столбец
// прокручивается сам; не помещаются — доска листается вбок. Ф20-4: чата оркестратора на главной нет (он на вкладке
// «Claude»), столбцы не уже 270 px — на ≥ 1280 все помещаются без прокрутки вбок. `phone` — столбец на всю ширину
// со свайпом, сверху чипы проектов.
// Обновление — при изменениях `/sync` (стор, троттлинг 2 с) и раз в 30 с (здесь, пока главная открыта).
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { UNSORTED_PROJECT_ID } from '../api/types';
import { subscribeHomePoll } from '../store/homePoll';
import { store } from '../store/instance';
import { buildBoard } from '../util/board';
import { safeColor } from '../util/color';
import ProjectColumn from './ProjectColumn.vue';

// Ф41-6a: на компьютере доска живёт внутри панели «Ждёт тебя» (`WaitBar`): `onlyProject` — один проект, `hiddenIds` —
// скрытые проекты (`hidden_projects` раскладки) в панели не показываются.
const props = withDefaults(defineProps<{ phone?: boolean; onlyProject?: number | null; hiddenIds?: number[] }>(), {
  phone: false,
  onlyProject: null,
  hiddenIds: () => [],
});

const home = computed(() => store.state.home);
const nowTick = ref(Date.now());
const boardEl = ref<HTMLElement | null>(null);
let poll: ReturnType<typeof subscribeHomePoll> | null = null;
let tickTimer: ReturnType<typeof setInterval> | null = null;

const board = computed(() =>
  buildBoard(
    store.state.projects,
    {
      ...home.value,
      joins: Object.values(store.state.joins.items),
      ruleRequests: Object.values(store.state.ruleRequests.items),
      agentRequests: Object.values(store.state.agentRequests.items),
      presses: Object.values(store.state.presses.items),
      humans: Object.values(store.state.humans.items),
      leaves: Object.values(store.state.leaves.items),
      groupOps: Object.values(store.state.groupOps.items),
      reminders: Object.values(store.state.reminders.items),
    },
    nowTick.value,
    store.state.sessions,
  ),
);
const allColumns = computed(() =>
  [...board.value.columns, ...(board.value.unsorted ? [board.value.unsorted] : [])].filter((c) => {
    const id = c.kind === 'project' ? c.project.id : UNSORTED_PROJECT_ID;
    return !props.hiddenIds.includes(id) && (props.onlyProject === null || props.onlyProject === id);
  }),
);

/** Дорожки сетки: столбцы проектов не уже 270 px, «Неразобранное» — узкое последним; на телефоне — по 100 %. */
const gridStyle = computed(() => {
  if (props.phone) return undefined;
  const n = allColumns.value.filter((c) => c.kind === 'project').length;
  const unsorted = allColumns.value.some((c) => c.kind !== 'project');
  const projects = n > 0 ? `repeat(${n}, minmax(270px, 1fr))` : '';
  return { gridTemplateColumns: `${projects}${unsorted ? ' minmax(270px, 1fr)' : ''}`.trim() || undefined };
});

function columnId(column: (typeof allColumns.value)[number]): number {
  return column.kind === 'project' ? column.project.id : UNSORTED_PROJECT_ID;
}

/** Якорь главной (кнопка «ждёт: N» полосы и т. п.): раздел первого столбца, где в нём есть содержимое; нет —
 * к доске. */
async function scrollToAnchor(): Promise<void> {
  const anchor = store.state.homeAnchor;
  if (anchor === null) return;
  await nextTick();
  const has = (column: (typeof allColumns.value)[number]): boolean => {
    if (anchor === 'react') return column.react.length > 0;
    if (anchor === 'drafts') return column.drafts.length > 0;
    if (anchor === 'sent') return column.sent.length > 0; // Ф11-4 И4: есть и у «Неразобранного»
    return column.kind === 'project' && column.runs.length > 0;
  };
  const column = allColumns.value.find(has);
  const target =
    column === undefined
      ? boardEl.value
      : (boardEl.value?.querySelector<HTMLElement>(
          `.board-column[data-project-id="${columnId(column)}"] [data-section="${anchor}"]`,
        ) ?? boardEl.value);
  target?.scrollIntoView?.({ block: 'start', inline: 'nearest' });
  if (target !== null && target !== boardEl.value) {
    target.classList.add('flash');
    setTimeout(() => target.classList.remove('flash'), 2000);
  }
  store.state.homeAnchor = null;
}

onMounted(() => {
  poll = subscribeHomePoll();
  void poll.loaded.then(scrollToAnchor);
  tickTimer = setInterval(() => {
    nowTick.value = Date.now();
  }, 30_000);
});

onBeforeUnmount(() => {
  poll?.release();
  if (tickTimer !== null) clearInterval(tickTimer);
});

watch(() => store.state.homeAnchor, () => void scrollToAnchor());

// --- телефон: чипы проектов над доской (A4) ---------------------------------------------------------------------

const activeIndex = ref(0);

function onBoardScroll(): void {
  if (!props.phone) return;
  const el = boardEl.value;
  if (el === null || el.clientWidth === 0) return;
  activeIndex.value = Math.min(allColumns.value.length - 1, Math.max(0, Math.round(el.scrollLeft / el.clientWidth)));
}

function showColumn(index: number): void {
  activeIndex.value = index;
  const column = allColumns.value[index];
  if (column === undefined) return;
  const el = boardEl.value?.querySelector<HTMLElement>(`.board-column[data-project-id="${columnId(column)}"]`);
  el?.scrollIntoView?.({ block: 'nearest', inline: 'start', behavior: 'smooth' });
}
</script>

<template>
  <main
    class="home-view"
    :class="{ 'home-view--phone': phone }"
    aria-label="Главная"
  >
    <p
      v-if="home.error"
      class="home-error"
      role="alert"
    >
      {{ home.error }}
    </p>
    <nav
      v-if="phone"
      class="board-chips"
      aria-label="Проекты доски"
    >
      <button
        v-for="(column, index) in allColumns"
        :key="columnId(column)"
        type="button"
        class="board-chip"
        :data-project-id="columnId(column)"
        :aria-current="index === activeIndex ? 'true' : undefined"
        @click="showColumn(index)"
      >
        <span
          v-if="column.kind === 'project'"
          class="color-dot"
          :style="{ backgroundColor: safeColor(column.project.color) }"
        />
        {{ column.kind === 'project' ? (column.project.name ?? '(без названия)') : 'Неразобранное' }}
        <span
          v-if="column.kind === 'project' && column.waitCount > 0"
          class="badge badge-react"
        >{{ column.waitCount }}</span>
      </button>
    </nav>
    <div
      ref="boardEl"
      class="board"
      :class="{ 'board--phone': phone }"
      :style="gridStyle"
      @scroll.passive="onBoardScroll"
    >
      <ProjectColumn
        v-for="column in allColumns"
        :key="columnId(column)"
        :column="column"
      />
    </div>
  </main>
</template>
