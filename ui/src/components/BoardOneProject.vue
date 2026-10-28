<script setup lang="ts">
// Ф47-1 (раздел 4): вид «Один проект» — строка «Проект:» с кнопками проектов и ряд столбцов: каждая сессия
// выбранного проекта своим столбцом (порядок — как в списке слева), в конце «+ Новая сессия в <проект>».
import { computed, nextTick, ref, watch } from 'vue';

import { boardState, FLASH_MS, itemKey, setViewProject } from '../store/board';
import { useBoardGroups } from '../store/boardGroups';
import { resolveViewProject, viewProjects } from '../util/boardViews';
import { safeColor } from '../util/color';
import BoardProjectWait from './BoardProjectWait.vue';
import DayControl from './DayControl.vue';
import BoardRowWindow from './BoardRowWindow.vue';
import NewSessionForm from './NewSessionForm.vue';

const props = defineProps<{ nowMs?: number }>();

const groups = useBoardGroups(() => props.nowMs);
const projects = computed(() => viewProjects(groups.value));
const current = computed(() => resolveViewProject(projects.value, boardState.layout.view_project));
const rowEl = computed(() => ({ '--board-n': String(boardState.layout.columns) }));
const showNew = ref(false);

// Другой проект — форма новой сессии прошлого закрывается.
watch(
  () => current.value?.id,
  () => {
    showNew.value = false;
  },
);

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
  <div class="board-oneproject">
    <div
      v-if="projects.length > 0"
      class="board-projbar"
      role="group"
      aria-label="Проект"
    >
      <span class="board-projbar-label">Проект:</span>
      <button
        v-for="p in projects"
        :key="p.id"
        type="button"
        class="board-projbar-btn"
        :aria-pressed="current?.id === p.id"
        :data-project-id="p.id"
        @click="setViewProject(p.id)"
      >
        <span
          class="color-dot"
          :style="{ backgroundColor: safeColor(p.color) }"
        />
        {{ p.name }}
        <span class="board-row-sub">{{ p.rows.length }}</span>
        <span
          v-if="p.waitCount > 0"
          class="badge badge-react"
        >{{ p.waitCount }}</span>
      </button>
    </div>
    <DayControl
      v-if="current !== null && current.id >= 1"
      :key="`day-${current.id}`"
      class="board-day"
      :project-id="current.id"
    />
    <BoardProjectWait
      v-if="current !== null"
      :key="current.id"
      :project-id="current.id"
      :count="current.waitCount"
    />
    <div
      class="board-columns board-columns--project"
      :style="rowEl"
    >
      <p
        v-if="current === null"
        class="board-columns-empty"
      >
        Сессий пока нет — заведи первую кнопкой «+» у проекта слева
      </p>
      <template v-else>
        <BoardRowWindow
          v-for="row in current.rows"
          :key="itemKey(row.item)"
          :item="row.item"
          :now-ms="nowMs"
        />
        <section
          v-if="current.id >= 1"
          class="board-new-col"
        >
          <button
            type="button"
            class="board-new-col-btn"
            :aria-expanded="showNew"
            @click="showNew = !showNew"
          >
            + Новая сессия в {{ current.name }}
          </button>
          <NewSessionForm
            v-if="showNew"
            :project-id="current.id"
            @done="showNew = false"
            @cancel="showNew = false"
          />
        </section>
      </template>
    </div>
  </div>
</template>
