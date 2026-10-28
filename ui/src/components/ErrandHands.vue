<script setup lang="ts">
// Ф96-1: руки посреди поручения агента. Руки у агента — «✋ Беру сам»; у владельца, причина
// передачи (если передал агент) и «▶ Продолжай» с необязательным «что сделал». Тексты — чужой ввод: интерполяция.
import { computed, ref } from 'vue';

import type { OwnerErrand } from '../api/types';
import { store } from '../store/instance';

const props = defineProps<{ errand: OwnerErrand; testPrefix: string }>();

const busy = ref(false);
const error = ref<string | null>(null);
const note = ref('');

const mine = computed(() => props.errand.hands === 'owner_channel');
const since = computed(() => {
  const ts = props.errand.hands_ts;
  if (ts === null || ts === undefined) return null;
  return new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
});

async function pass(to: 'owner_channel' | 'agent'): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.errandHands(props.errand.id, to, to === 'agent' ? note.value : undefined);
    if (!result.ok) error.value = result.message;
    else note.value = '';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="errand-hands"
    :data-test="`${testPrefix}-hands`"
  >
    <button
      v-if="!mine"
      type="button"
      class="link-button"
      :data-test="`${testPrefix}-hands-take`"
      :disabled="busy"
      @click="pass('owner_channel')"
    >
      ✋ Беру сам
    </button>
    <template v-else>
      <p
        class="errand-hands-line"
        :data-test="`${testPrefix}-hands-line`"
      >
        руки у тебя<template v-if="since"> с {{ since }}</template>
      </p>
      <p
        v-if="errand.hands_by === 'agent' && errand.hands_why"
        class="errand-hands-line"
        :data-test="`${testPrefix}-hands-why`"
      >
        агент передал: {{ errand.hands_why }}
      </p>
      <div class="errand-hands-row">
        <input
          v-model="note"
          type="text"
          class="errand-hands-note"
          maxlength="2000"
          aria-label="Что сделал"
          placeholder="что сделал (необязательно)"
          :data-test="`${testPrefix}-hands-note`"
          @keydown.enter.prevent="pass('agent')"
        >
        <button
          type="button"
          class="link-button"
          :data-test="`${testPrefix}-hands-back`"
          :disabled="busy"
          @click="pass('agent')"
        >
          ▶ Продолжай
        </button>
      </div>
    </template>
    <p
      v-if="error"
      class="errand-block-error"
      role="alert"
      :data-test="`${testPrefix}-hands-error`"
    >
      {{ error }}
    </p>
  </div>
</template>
