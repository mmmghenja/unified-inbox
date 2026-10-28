<script setup lang="ts">
// Ф41-6a (`docs/architecture.md`) + Ф90-2 (Р3): панель «Ждёт тебя» над доской. Кнопка-счётчик живёт в
// верхней полосе (`TopBar`), здесь — только панель: поверх доски (в поток не входит, доску не сдвигает), кнопки по
// проектам («Globex 3 ▾») и вся прежняя доска проектов (`HomeView`, те же `ProjectColumn`) — ничего не потеряно.
// Закрывается Esc, кликом мимо (по подложке над доской) и «Закрыть». N — тот же счёт, что у бейджа вкладки «Ждёт»
// телефона (`buildWaiting`).
import { computed, onBeforeUnmount, watch } from 'vue';

import { boardState, toggleWait } from '../store/board';
import { store } from '../store/instance';
import { waitSummary } from '../util/boardList';
import { safeColor } from '../util/color';
import { buildWaiting } from '../util/waiting';
import { waitingInputOf } from '../util/waitInput';
import ClosedDayLines from './ClosedDayLines.vue';
import HomeView from './HomeView.vue';

const waiting = computed(() =>
  buildWaiting(waitingInputOf(store)),
);
const hidden = computed(() => boardState.layout.hidden_projects);
const summary = computed(() => waitSummary(waiting.value, hidden.value, store.state.projects));
const open = computed(() => boardState.waitShown);

function colorOf(id: number): string | null {
  return store.state.projects.find((p) => p.id === id)?.color ?? null;
}

function onKey(e: KeyboardEvent): void {
  if (e.key === 'Escape') toggleWait(null);
}

watch(
  open,
  (now) => {
    if (now) document.addEventListener('keydown', onKey);
    else document.removeEventListener('keydown', onKey);
  },
  { immediate: true },
);
onBeforeUnmount(() => document.removeEventListener('keydown', onKey));
</script>

<template>
  <section
    class="wait-bar"
    :class="{ 'wait-bar--open': open }"
    aria-label="Ждёт тебя"
  >
    <template v-if="open">
      <div
        class="wait-bar-backdrop"
        data-test="wait-backdrop"
        @click="toggleWait(null)"
      />
      <div class="wait-bar-pop">
        <div class="wait-bar-head">
          <span
            class="wait-bar-title"
            :class="{ 'wait-bar-title--none': summary.total === 0 }"
          >{{ summary.total > 0 ? `Ждёт тебя · ${summary.total}` : 'Ничего не ждёт' }}</span>
          <button
            v-for="entry in summary.byProject"
            :key="entry.id"
            type="button"
            class="wait-bar-project"
            :aria-pressed="boardState.waitProject === entry.id"
            :data-project-id="entry.id"
            @click="toggleWait(boardState.waitProject === entry.id ? 'all' : entry.id)"
          >
            <span
              v-if="entry.id > 0"
              class="color-dot"
              :style="{ backgroundColor: safeColor(colorOf(entry.id)) }"
            />
            {{ entry.name }} {{ entry.count }} ▾
          </button>
          <span class="wait-bar-spacer" />
          <button
            type="button"
            class="link-button wait-bar-toggle"
            @click="toggleWait(null)"
          >
            Закрыть
          </button>
        </div>
        <ClosedDayLines />
        <div class="wait-bar-panel">
          <HomeView
            :only-project="boardState.waitProject"
            :hidden-ids="hidden"
          />
        </div>
      </div>
    </template>
  </section>
</template>
