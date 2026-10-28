<script setup lang="ts">
// Ф64-1 (раздел 6): рабочий день проекта — метка «день закрыт с ЧЧ:ММ» и кнопка «Закончить день» / «Начать день».
// Только у проектов с `workday`; остальным ничего не рисует. Один компонент на вкладку проекта, шапку столбца
// (все виды главной) и телефон. «Закончить день» — с подтверждением; ошибка ядра показывается её текстом.
import { computed, ref } from 'vue';

import { store } from '../store/instance';
import { CLOSE_DAY_CONFIRM, dayClosedLabel } from '../util/workday';

const props = defineProps<{ projectId: number }>();

const project = computed(() => store.state.projects.find((p) => p.id === props.projectId));
const label = computed(() => dayClosedLabel(project.value));
const closed = computed(() => project.value?.day_open === false);
const busy = ref(false);
const error = ref<string | null>(null);

async function toggle(): Promise<void> {
  if (busy.value) return;
  if (!closed.value && !window.confirm(CLOSE_DAY_CONFIRM)) return;
  busy.value = true;
  error.value = null;
  const result = await store.setProjectDay(props.projectId, closed.value);
  busy.value = false;
  if (!result.ok) error.value = result.message;
}
</script>

<template>
  <span
    v-if="project?.workday === true"
    class="day-control"
    data-test="day-control"
  >
    <span
      v-if="label !== null"
      class="day-closed-label"
      data-test="day-label"
    >{{ label }}</span>
    <button
      type="button"
      class="link-button day-toggle"
      data-test="day-toggle"
      :disabled="busy"
      @click="toggle"
    >{{ closed ? 'Начать день' : 'Закончить день' }}</button>
    <span
      v-if="error !== null"
      class="day-error"
      role="alert"
      data-test="day-error"
    >{{ error }}</span>
  </span>
</template>
