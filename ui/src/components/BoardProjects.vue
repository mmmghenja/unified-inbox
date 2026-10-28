<script setup lang="ts">
// Ф47-1 (раздел 3): вид «По проектам» — ряд столбцов, по столбцу на проект (`BoardProject`). Проекты — группы
// списка слева с сессиями, скрытые не показываются. Ширина столбца — 1/N, как у своего набора; лишнее — вбок.
import { computed, nextTick, watch } from 'vue';

import { boardState, FLASH_MS } from '../store/board';
import { useBoardGroups } from '../store/boardGroups';
import { viewProjects } from '../util/boardViews';
import BoardProject from './BoardProject.vue';

const props = defineProps<{ nowMs?: number }>();

const groups = useBoardGroups(() => props.nowMs);
const projects = computed(() => viewProjects(groups.value));
const rowEl = computed(() => ({ '--board-n': String(boardState.layout.columns) }));

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
    class="board-columns board-columns--projects"
    :style="rowEl"
  >
    <p
      v-if="projects.length === 0"
      class="board-columns-empty"
    >
      Сессий пока нет — заведи первую кнопкой «+» у проекта слева
    </p>
    <BoardProject
      v-for="project in projects"
      :key="project.id"
      :project="project"
      :now-ms="nowMs"
    />
  </div>
</template>
