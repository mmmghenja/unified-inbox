<script setup lang="ts">
// Чип шапки сессии: «<модель> · <усилие>»; клик открывает форму «Модель и усилие…». Модель и усилие — ввод владельца:
// только текстом. Длинное имя обрезается многоточием.
import { computed } from 'vue';

import type { HubSession } from '../api/types';
import { modelChipText } from '../util/sessions';

const props = defineProps<{ session: Pick<HubSession, 'model' | 'effort'> }>();
defineEmits<{ click: [] }>();

const text = computed(() => modelChipText(props.session));
</script>

<template>
  <button
    type="button"
    class="session-model-chip"
    title="Модель и усилие"
    @click="$emit('click')"
  >
    {{ text }}
  </button>
</template>
