<script setup lang="ts">
// Ф41-6a (2.4): общая оболочка столбца доски — шапка («Проект /» серым, имя жирным, чип состояния, «⋯» с «Левее» и
// «Правее», «×» — убрать с доски, сущность не трогает), середина со своей прокруткой. Всё в шапке — чужой ввод
// (имена проектов, ролей): только текстом.
import { ref } from 'vue';

import { closeColumn, itemKey, moveColumn, type ColumnItem } from '../store/board';
import { usePopup } from '../util/popup';

const props = defineProps<{ item: ColumnItem; project?: string | null; title: string; chip?: string | null; chipTone?: 'wait' | 'run' | 'idle'; fill?: boolean; auto?: boolean; fixed?: boolean; dot?: 'wait' | 'run' | 'idle' | 'silent'; dotText?: string }>();
// Ф90-1: `dot` — шапка в одну строку: точка состояния (подпись — по наведению) вместо чипа и «Проект /».
const emit = defineEmits<{ auto: [] }>();
// Ф47-1: `fixed` — столбец не свой (виды «По проектам» и «Один проект»): ни «закрыть», ни «подвинуть».

const menu = ref(false);
const menuButton = ref<HTMLElement | null>(null);
const menuPanel = ref<HTMLElement | null>(null);
// Ф80-1 A2: Esc, нажатие мимо и открытие другого меню закрывают это меню (общий механизм `util/popup`).
usePopup({ open: menu, button: () => menuButton.value, panel: () => menuPanel.value });

function move(delta: number): void {
  menu.value = false;
  moveColumn(props.item, delta);
}
</script>

<template>
  <section
    class="board-col"
    :data-col-key="itemKey(item)"
    :aria-label="title"
  >
    <header
      class="board-col-head"
      :class="{ 'board-col-head--one': dot !== undefined }"
    >
      <span
        v-if="dot !== undefined"
        class="board-dot board-col-dot"
        :class="`board-dot--${dot}`"
        role="img"
        :title="dotText"
        :aria-label="dotText"
      />
      <span class="board-col-title">
        <span
          v-if="project"
          class="board-col-project"
        >{{ project }} /</span>
        <b class="board-col-name">{{ title }}</b>
      </span>
      <span
        v-if="chip"
        class="board-col-chip"
        :class="`board-col-chip--${chipTone ?? 'idle'}`"
      >{{ chip }}</span>
      <slot name="head-extra" />
      <button
        v-if="auto"
        type="button"
        class="board-col-auto"
        title="Авто-режим: нажми, чтобы вернуть ручной"
        @click="emit('auto')"
      >
        авто
      </button>
      <span class="board-col-spacer" />
      <button
        v-if="!fixed || $slots['menu-extra']"
        ref="menuButton"
        type="button"
        class="board-col-menu"
        aria-label="Действия со столбцом"
        :aria-expanded="menu"
        @click="menu = !menu"
      >
        ⋯
      </button>
      <button
        v-if="!fixed"
        type="button"
        class="board-col-close"
        aria-label="Убрать столбец"
        @click="closeColumn(item)"
      >
        ×
      </button>
      <div
        v-if="menu"
        ref="menuPanel"
        class="board-col-popup"
        role="menu"
      >
        <button
          v-if="!fixed"
          type="button"
          role="menuitem"
          class="board-col-left"
          @click="move(-1)"
        >
          Левее
        </button>
        <button
          v-if="!fixed"
          type="button"
          role="menuitem"
          class="board-col-right"
          @click="move(1)"
        >
          Правее
        </button>
        <template v-if="$slots['menu-extra']">
          <hr
            v-if="!fixed"
            class="board-col-sep"
          >
          <slot
            name="menu-extra"
            :close="() => (menu = false)"
          />
        </template>
      </div>
    </header>
    <div
      class="board-col-body"
      :class="{ 'board-col-body--fill': fill }"
    >
      <slot />
    </div>
  </section>
</template>
