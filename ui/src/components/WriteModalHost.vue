<script setup lang="ts">
// Ф58-1 B: хозяин окна «Написать…» (`store/writeModal.ts`). Монтируется в `MainApp.vue`: окно открывает любой
// компонент (верхняя полоса, окно «Агент», карточка очереди), не таща его с собой.
import { writeModal } from '../store/writeModal';
import WriteModal from './WriteModal.vue';
</script>

<template>
  <WriteModal v-if="writeModal.open" />
</template>
