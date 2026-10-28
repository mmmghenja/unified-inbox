<script setup lang="ts">
// Ф14-2 D1 (docs/architecture.md: одна кнопка «Новое сообщение» — в верхней полосе
// (`TopBar.vue`) и на телефоне в «Беседах» (`PhoneShell.vue`). Форма — поверх всего (`NewMessageForm.vue`); проект
// по умолчанию — проект открытой вкладки, если это настоящий проект, иначе «по правилам».
import { computed, ref } from 'vue';

import { store } from '../store/instance';
import NewMessageForm from './NewMessageForm.vue';

const open = ref(false);

const defaultProjectId = computed(() => {
  const projectId = store.state.currentProjectId;
  return !store.state.homeOpen && projectId !== null && projectId > 0 ? projectId : null;
});
</script>

<template>
  <button
    type="button"
    class="new-message-button"
    :aria-expanded="open"
    @click="open = true"
  >
    Новое сообщение
  </button>
  <div
    v-if="open"
    class="new-message-overlay"
  >
    <NewMessageForm
      :project-id="defaultProjectId"
      @done="open = false"
      @cancel="open = false"
    />
  </div>
</template>
