<script setup lang="ts">
// Ф41-6a (2.2) + Ф90-2 (Р3): одна тонкая строка над доской. Слева «☰» (свернуть/показать список), справа мелкая
// сводка «сессий в работе X из Y» (на ширине < 1280 прячется стилем) и «Вид ▾» — выпадашка: вид «Свой набор / По
// проектам / Один проект» и число столбцов.
// Сумма расхода за день ($Z) — из `today.cost_usd` ответа `GET /sessions`; пока ядро поля не отдаёт — без суммы.
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import { boardState, MAX_COLUMNS, MIN_COLUMNS, setColumns, setView, toggleList, type BoardViewKind } from '../store/board';
import { store } from '../store/instance';
import { buildAgentLines } from '../store/peerMsgs';
import { workSummary } from '../util/boardList';

const VIEWS: { key: BoardViewKind; label: string }[] = [
  { key: 'own', label: 'Свой набор' },
  { key: 'projects', label: 'По проектам' },
  { key: 'project', label: 'Один проект' },
];

const menuOpen = ref(false);
const root = ref<HTMLElement | null>(null);

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') menuOpen.value = false;
}
function onDocClick(e: MouseEvent): void {
  if (root.value !== null && e.target instanceof Node && !root.value.contains(e.target)) menuOpen.value = false;
}
watch(menuOpen, (now) => {
  if (now) {
    document.addEventListener('keydown', onKey);
    document.addEventListener('click', onDocClick);
  } else {
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('click', onDocClick);
  }
});
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKey);
  document.removeEventListener('click', onDocClick);
});

const layout = computed(() => boardState.layout);
const cost = computed(() => store.state.sessions.today?.cost_usd ?? null);
const summary = computed(() =>
  workSummary(
    Object.values(store.state.sessions.items),
    buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs),
  ),
);
</script>

<template>
  <div
    ref="root"
    class="board-toolbar"
    role="toolbar"
    aria-label="Доска"
  >
    <button
      type="button"
      class="board-toolbar-list"
      :title="layout.list_collapsed ? 'Показать список' : 'Свернуть список'"
      :aria-label="layout.list_collapsed ? 'Показать список' : 'Свернуть список'"
      :aria-pressed="!layout.list_collapsed"
      @click="toggleList"
    >
      ☰
    </button>
    <span class="board-toolbar-spacer" />
    <span class="board-toolbar-summary">сессий в работе {{ summary.running }} из {{ summary.total }}<template v-if="cost !== null"> · ${{ cost.toFixed(2) }} сегодня</template></span>
    <button
      type="button"
      class="board-toolbar-viewbtn"
      aria-haspopup="true"
      :aria-expanded="menuOpen"
      @click="menuOpen = !menuOpen"
    >
      Вид ▾
    </button>
    <div
      v-if="menuOpen"
      class="board-toolbar-menu"
    >
      <span
        class="board-toolbar-view"
        role="group"
        aria-label="Вид"
      >
        <button
          v-for="v in VIEWS"
          :key="v.key"
          type="button"
          class="board-view-btn"
          :data-view="v.key"
          :aria-pressed="layout.view === v.key"
          @click="setView(v.key)"
        >{{ v.label }}</button>
      </span>
      <span class="board-toolbar-cols">
        Столбцов на экране:
        <button
          type="button"
          class="board-cols-minus"
          aria-label="Меньше столбцов"
          :disabled="layout.columns <= MIN_COLUMNS"
          @click="setColumns(layout.columns - 1)"
        >−</button>
        <b class="board-cols-count">{{ layout.columns }}</b>
        <button
          type="button"
          class="board-cols-plus"
          aria-label="Больше столбцов"
          :disabled="layout.columns >= MAX_COLUMNS"
          @click="setColumns(layout.columns + 1)"
        >+</button>
      </span>
    </div>
  </div>
</template>
