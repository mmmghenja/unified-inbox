<script setup lang="ts">
// Ф59-1 (docs/architecture.md): «🎤 Надиктовать» рядом с полем ввода. Нажал — запись
// микрофона, куски речи уходят в ядро (`POST /dictate`), готовый текст вставляется в позицию курсора поля
// (поле получает `input`, `v-model` подхватывает). На месте кнопки — плашка: уровень, таймер, «Стоп»; повторное
// нажатие «Стоп» отдаёт хвост и заканчивает. Ядро ответило `enabled: false` — кнопки нет.
//
// Поле: проп `target`; не задан — элемент ровно перед кнопкой (textarea/input), так кнопку ставят следом за полем.
// От кнопки голосового сообщения (`VoiceRecorder`) отличается подписью «Надиктовать» и тем, что стоит у поля.
import { computed, ref } from 'vue';

import { useDictateEnabled, useDictation, dictationSupported, type DictateField } from '../util/dictation/useDictation';
import { formatElapsed } from '../util/dictation/capture';

const props = defineProps<{
  target?: DictateField | null;
}>();

const root = ref<HTMLElement | null>(null);

function isField(el: Element | null | undefined): el is DictateField {
  return el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement;
}

function resolveTarget(): DictateField | null {
  if (props.target) return props.target;
  const prev = root.value?.previousElementSibling;
  return isField(prev) ? prev : null;
}

const supported = dictationSupported();
// Без микрофона в этом окне (не безопасный контекст, старый браузер) ядро не спрашиваем вовсе.
const enabled = supported ? useDictateEnabled() : ref<boolean | null>(false);
const { listening, pending, level, elapsed, error, start, stop } = useDictation(resolveTarget);

const visible = computed(() => supported && (enabled.value === true || error.value !== null));
const busy = computed(() => listening.value || pending.value);
const status = computed(() =>
  listening.value ? 'Слушаю — текст появится после паузы' : 'Распознаю…',
);
const levelStyle = computed(() => ({ width: `${Math.round(level.value * 100)}%` }));
</script>

<template>
  <span
    ref="root"
    class="dictate"
  >
    <template v-if="visible">
      <button
        v-if="!busy"
        type="button"
        class="dictate-btn"
        title="Надиктовать текст в это поле"
        aria-label="Надиктовать текст"
        @mousedown.prevent
        @click="start"
      >
        <span aria-hidden="true">🎤</span>
        <span class="dictate-label">Надиктовать</span>
      </button>
      <span
        v-else
        class="dictate-pill"
        role="status"
      >
        <span
          class="dictate-level"
          aria-hidden="true"
        ><span
          class="dictate-level-fill"
          :style="levelStyle"
        /></span>
        <span
          v-if="listening"
          class="dictate-time"
        >{{ formatElapsed(elapsed * 1000) }}</span>
        <span class="dictate-status">{{ status }}</span>
        <button
          v-if="listening"
          type="button"
          class="dictate-stop"
          @mousedown.prevent
          @click="stop"
        >
          Стоп
        </button>
      </span>
      <span
        v-if="error"
        class="dictate-error"
        role="alert"
      >{{ error }}</span>
    </template>
  </span>
</template>
