<script setup lang="ts">
// Ф64-1 (раздел 6): тихие строки «<проект>: день закрыт · ждёт N» под «Ждёт тебя» — то, что скрыто закрытым днём.
// Без кнопок: день открывают кнопкой «Начать день» у проекта.
import { computed } from 'vue';

import { store } from '../store/instance';
import { closedDayLines, closedDayText } from '../util/waiting';
import { waitingInputOf } from '../util/waitInput';

const lines = computed(() => closedDayLines(waitingInputOf(store)));
</script>

<template>
  <p
    v-for="line in lines"
    :key="line.project.id ?? 'unsorted'"
    class="day-closed-line"
    data-test="closed-day-line"
  >
    {{ closedDayText(line) }}
  </p>
</template>
