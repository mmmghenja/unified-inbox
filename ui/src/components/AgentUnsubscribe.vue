<script setup lang="ts">
// «Отписать от хаба» у внешней сессии с живой подпиской: сессия перестаёт получать сигналы и пропадает из списка.
import { ref } from 'vue';

import { store } from '../store/instance';

const props = defineProps<{ subId: number }>();
const emit = defineEmits<{ done: [] }>();

const CONFIRM_TEXT = 'Сессия перестанет получать сигналы хаба и пропадёт из списка. Саму сессию это не закрывает.';

const busy = ref(false);
const error = ref<string | null>(null);

async function unsubscribe(): Promise<void> {
  if (busy.value) return;
  if (!window.confirm(CONFIRM_TEXT)) return;
  busy.value = true;
  error.value = null;
  const result = await store.endSub(props.subId);
  busy.value = false;
  if (!result.ok) error.value = result.message;
  else emit('done');
}
</script>

<template>
  <button
    type="button"
    class="board-agent-adopt"
    data-test="agent-unsubscribe"
    :disabled="busy"
    title="Отписать сессию от хаба"
    @click="unsubscribe"
  >
    Отписать от хаба
  </button>
  <p
    v-if="error !== null"
    class="board-agent-error"
    role="alert"
  >
    {{ error }}
  </p>
</template>
