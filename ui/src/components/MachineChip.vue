<script setup lang="ts">
// Ф77-1: серый чип машины роли (как чип движка). Нет машины (старое ядро, внешняя линия «не в сети») — ничего.
// Текст — `display`, подсказка — hostname, путь и последний контакт. Имя — чужой ввод: текстом.
import { computed } from 'vue';

import type { Machine } from '../api/types';
import { machineTitle } from '../util/machines';

const props = defineProps<{ machine?: Machine | null }>();
const title = computed(() => (props.machine ? machineTitle(props.machine) : undefined));
</script>

<template>
  <span
    v-if="machine"
    class="machine-chip"
    data-test="machine-chip"
    :title="title"
  >{{ machine.display }}</span>
</template>
