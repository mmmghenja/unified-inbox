<script setup lang="ts">
// Ф65-1 (И2, `docs/architecture.md`): карточка, которую агент вернул владельцу. Заголовок («Агент
// вернул тебе: …» / «Агент завис: …» / «остановил владелец»), под ним подробности (`detail` — «Что пробовал / Почему
// вернул / Что нужно от тебя», свёрнуты до трёх строк), поле «скажи агенту, как решить» и обычная кнопка «Поручить
// агенту» рядом (агент берёт карточку снова; текст — его указание, без текста — то же «Реши»), «разговор с агентом».
// Правки 15.10 (раздел 10 спеки): владелец не понял прежнее «сказать агенту (необязательно)» + широкое «Пусть решает».
// Один компонент на карточку очереди и заявку «нужен человек». Тексты — чужой ввод: только интерполяцией.
import { computed, ref } from 'vue';

import type { OwnerErrandReturned } from '../api/types';
import { onSubmitKey, submitKeyLabel } from '../util/submitKeys';
import { RETURNED_DETAIL_LINES, returnedLine } from '../util/owner_channel';

const props = defineProps<{ returned: OwnerErrandReturned; busy?: boolean; testPrefix: string }>();
const emit = defineEmits<{ say: [text: string]; talk: [] }>();

const head = computed(() => returnedLine(props.returned) ?? '');
const detail = computed(() => (typeof props.returned.detail === 'string' ? props.returned.detail.trim() : ''));
const long = computed(() => detail.value.split('\n').length > RETURNED_DETAIL_LINES || detail.value.length > 240);
const open = ref(false);
const text = ref('');
const sendButton = ref<HTMLButtonElement | null>(null);
const keyLabel = submitKeyLabel();
const hint = 'Скажи агенту, как решить — например: «накатывай»';

function submit(): void {
  if (props.busy === true) return;
  emit('say', text.value.trim());
}

defineExpose({ clear: () => (text.value = '') });
</script>

<template>
  <div class="errand-returned">
    <p class="errand-returned-head">
      <span :data-test="`${testPrefix}-returned`">{{ head }}</span>
      <button
        type="button"
        class="link-button"
        :data-test="`${testPrefix}-returned-talk`"
        @click="emit('talk')"
      >
        разговор с агентом
      </button>
    </p>
    <template v-if="detail">
      <p
        class="errand-returned-detail"
        :class="{ 'errand-returned-detail--clamped': long && !open }"
        :data-test="`${testPrefix}-returned-detail`"
      >
        {{ detail }}
      </p>
      <button
        v-if="long"
        type="button"
        class="link-button"
        :data-test="`${testPrefix}-returned-more`"
        @click="open = !open"
      >
        {{ open ? 'свернуть' : 'развернуть' }}
      </button>
    </template>
    <div class="errand-returned-say">
      <textarea
        v-model="text"
        rows="2"
        class="owner-text-input"
        :aria-label="hint"
        :placeholder="hint"
        :data-test="`${testPrefix}-say`"
        @keydown="onSubmitKey($event, sendButton)"
      />
      <button
        ref="sendButton"
        type="button"
        class="owner-answer"
        :data-test="`${testPrefix}-let-decide`"
        :title="text.trim() === '' ? `Без текста — как «Реши» (${keyLabel})` : keyLabel"
        :disabled="busy === true"
        @click="submit"
      >
        Поручить агенту
      </button>
    </div>
  </div>
</template>
