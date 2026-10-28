<script setup lang="ts">
// Ф48-2 E (`docs/architecture.md`): блок «Ждёт тебя · N» вверху столбца проекта в видах
// «По проектам» и «Один проект». Состав и строки — те же, что у раскрытой общей полосы `WaitBar` по проекту (`HomeView`
// с `onlyProject`), сбор не дублируется. N — `waitCount` проекта (тот же счёт, что в шапке). Пусто — блока нет.
// Свёрнутость помнится по проекту через `util/uiPrefs.ts`. Высота — до трети столбца, дальше прокрутка.
import { ref } from 'vue';

import { readUiFlag, writeUiFlag } from '../util/uiPrefs';
import HomeView from './HomeView.vue';

const props = defineProps<{ projectId: number; count: number }>();

const key = (): `hub.${string}` => `hub.board_wait_closed.${props.projectId}`;
const closed = ref(readUiFlag(key()) === true);

function toggle(): void {
  closed.value = !closed.value;
  writeUiFlag(key(), closed.value);
}
</script>

<template>
  <section
    v-if="count > 0"
    class="board-wait"
    :data-wait-project="projectId"
    aria-label="Ждёт тебя"
  >
    <button
      type="button"
      class="link-button board-wait-head"
      :aria-expanded="!closed"
      @click="toggle"
    >
      Ждёт тебя · {{ count }} {{ closed ? '▸' : '▾' }}
    </button>
    <div
      v-if="!closed"
      class="board-wait-body"
    >
      <HomeView
        :only-project="projectId"
        :hidden-ids="[]"
      />
    </div>
  </section>
</template>
