<script setup lang="ts">
// Ф47-1 (`docs/architecture.md`): столбец проекта в виде «По проектам». Шапка (точка и имя
// проекта, «ждёт тебя · N», «Скрыть»), строка всех сессий проекта кнопками, «+» — новая сессия в проекте, ниже — окно
// выбранной сессии. Подписи кнопок — те же строки, что в списке слева. Всё — чужой ввод: только текстом.
import { computed, ref } from 'vue';

import { boardState, hideProject, itemKey, pickSession, sameItem, toggleWait } from '../store/board';
import { pickedRow, type ViewProject } from '../util/boardViews';
import { safeColor } from '../util/color';
import BoardProjectWait from './BoardProjectWait.vue';
import DayControl from './DayControl.vue';
import BoardRowWindow from './BoardRowWindow.vue';
import NewSessionForm from './NewSessionForm.vue';

const props = defineProps<{ project: ViewProject; nowMs?: number }>();

const showNew = ref(false);
const picked = computed(() => pickedRow(props.project, boardState.layout.picks));
// Проект 0 («вне проектов») — не настоящий: ни «Скрыть», ни «+».
const real = computed(() => props.project.id >= 1);
</script>

<template>
  <section
    class="board-project"
    :data-col-key="`project:${project.id}`"
    :aria-label="project.name"
  >
    <header class="board-project-head">
      <span
        class="color-dot"
        :style="{ backgroundColor: safeColor(project.color) }"
      />
      <b class="board-project-name">{{ project.name }}</b>
      <button
        v-if="project.waitCount > 0"
        type="button"
        class="link-button board-project-wait"
        @click="toggleWait(project.id)"
      >
        ждёт тебя · {{ project.waitCount }}
      </button>
      <DayControl
        v-if="real"
        :project-id="project.id"
      />
      <span class="board-col-spacer" />
      <button
        v-if="real"
        type="button"
        class="link-button board-project-hide"
        @click="hideProject(project.id)"
      >
        Скрыть
      </button>
    </header>
    <BoardProjectWait
      :project-id="project.id"
      :count="project.waitCount"
    />
    <div
      class="board-picks"
      role="group"
      aria-label="Сессии проекта"
    >
      <button
        v-for="row in project.rows"
        :key="itemKey(row.item)"
        type="button"
        class="board-pick"
        :aria-pressed="sameItem(row.item, picked.item)"
        :data-pick-key="itemKey(row.item)"
        @click="pickSession(project.id, row.item)"
      >
        <span class="board-pick-main">
          <span
            class="board-dot"
            :class="`board-dot--${row.dot}`"
          />
          <span class="board-row-name">{{ row.name }}</span>
        </span>
        <span class="board-row-sub">{{ row.sub }}</span>
      </button>
      <button
        v-if="real"
        type="button"
        class="board-pick board-pick-new"
        title="Новая сессия"
        aria-label="Новая сессия"
        :aria-expanded="showNew"
        @click="showNew = !showNew"
      >
        +
      </button>
    </div>
    <NewSessionForm
      v-if="showNew"
      :project-id="project.id"
      @done="showNew = false"
      @cancel="showNew = false"
    />
    <div class="board-project-window">
      <BoardRowWindow
        :key="itemKey(picked.item)"
        :item="picked.item"
        :now-ms="nowMs"
      />
    </div>
  </section>
</template>
