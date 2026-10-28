<script setup lang="ts">
// Ф48-1: чип «агенты: N работают» рядом с состоянием сессии; клик открывает окно со списком задач. Нет задач
// (`bg` пуст или поля нет) — ничего не рисуется.
import { computed, ref } from 'vue';

import type { HubSession } from '../api/types';
import { bgChipText, hasLiveBg } from '../util/sessionBg';
import { sessionName } from '../util/sessions';
import SessionBgModal from './SessionBgModal.vue';

const props = defineProps<{ session: HubSession }>();

const open = ref(false);
const text = computed(() => bgChipText(props.session));
const live = computed(() => hasLiveBg(props.session));
</script>

<template>
  <button
    v-if="text !== null"
    type="button"
    class="session-bg-chip"
    :class="{ 'session-bg-chip--live': live }"
    title="Агенты сессии"
    @click="open = true"
  >
    {{ text }}
  </button>
  <SessionBgModal
    v-if="open"
    :session="session"
    :title="`Агенты сессии ${sessionName(session)}`"
    @close="open = false"
  />
</template>
