<script setup lang="ts">
// Ф41-3а: содержимое строки ленты сессии, у которой есть `brief`/подробности: короткая строка шага и (по клику)
// diff, вывод команды или полный вход инструмента. Подробности грузятся одним запросом на событие и держатся в
// сторе; открытое состояние локально и переживает догрузку событий (строка в ленте с постоянным ключом).
import { computed, ref } from 'vue';

import type { SessionEvent } from '../api/types';
import { store } from '../store/instance';
import { detailKey } from '../store/sessions';
import { COST_TITLE_CLAUDE, stepView } from '../util/sessionSteps';
import DiffView from './DiffView.vue';

const props = defineProps<{ event: SessionEvent; sessionId: number; engine?: string }>();

const open = ref(false);
const view = computed(() => stepView(props.event));
// У раскрываемой строки значок раскрытия свой — «▸» из текста шага убирается (иначе «▸ ▸ Bash: …»).
const toggleText = computed(() => view.value.text.replace(/^▸\s*/, ''));
const entry = computed(() => store.state.sessions.details[detailKey(props.sessionId, props.event.id)]);
const detail = computed(() => (entry.value?.status === 'ok' ? entry.value.detail : null));
const title = computed(() =>
  props.event.kind === 'turn_end' && props.engine === 'claude' && props.event.brief?.type === 'usage'
    ? COST_TITLE_CLAUDE
    : undefined,
);

function inputJson(input: unknown): string {
  try {
    return JSON.stringify(input, null, 2) ?? '';
  } catch {
    return String(input);
  }
}

async function toggle(): Promise<void> {
  open.value = !open.value;
  if (open.value) await store.loadSessionEventDetail(props.sessionId, props.event.id);
}
</script>

<template>
  <button
    v-if="view.expandable"
    type="button"
    class="step-toggle"
    :aria-expanded="open"
    @click="toggle"
  >
    <span class="step-mark">{{ open ? '▾' : '▸' }}</span>
    <span class="step-text">{{ toggleText }}</span>
  </button>
  <span
    v-else
    class="step-text"
    :title="title"
  >{{ view.text }}</span>
  <div
    v-if="open"
    class="step-detail"
  >
    <p
      v-if="entry === undefined || entry.status === 'loading'"
      class="step-note"
    >
      загружаю…
    </p>
    <p
      v-else-if="entry.status === 'error'"
      class="step-note step-note--error"
    >
      подробности не загрузились — нажми ещё раз
    </p>
    <p
      v-else-if="detail === null"
      class="step-note"
    >
      подробностей нет
    </p>
    <template v-else-if="detail.type === 'diff'">
      <DiffView
        :patch="String(detail.patch ?? '')"
        :cut="detail.cut === true"
      />
    </template>
    <template v-else-if="detail.type === 'output'">
      <pre class="step-pre">{{ String(detail.text ?? '') }}</pre>
      <p
        v-if="detail.cut === true"
        class="diff-cut"
      >
        вывод обрезан
      </p>
    </template>
    <template v-else-if="detail.type === 'input'">
      <p
        v-if="typeof detail.description === 'string' && detail.description !== ''"
        class="step-note"
      >
        {{ detail.description }}
      </p>
      <pre
        v-if="typeof detail.command === 'string'"
        class="step-pre"
      >{{ detail.command }}</pre>
      <pre
        v-else
        class="step-pre"
      >{{ inputJson(detail.input) }}</pre>
    </template>
    <p
      v-else
      class="step-note"
    >
      подробностей нет
    </p>
  </div>
</template>
