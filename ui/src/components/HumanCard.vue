<script setup lang="ts">
// Ф54-1 (`docs/architecture.md`): карточка «нужен человек» — сессия просит владельца сделать то,
// что агент не может (капча, вход, код, оплата, согласие). «Готово» / «Не вышло» / «Не сейчас» — только из интерфейса.
// Все тексты автора (site, url, need, doing, profile, tab, role) — чужой ввод: только текстовой интерполяцией;
// `url` — простым текстом с кнопкой «копировать», ссылкой не делается. Скриншот — миниатюра, по клику окном поверх.
// Ф58-3: заявку решает агент (поле `errand` выдачи ядра) — строка «решает агент: …» и «Остановить агента».
// Ф65-1: кнопки владельца видны всегда (его ответ закрывает поручение сам); агент вернул (`errand_returned`) — блок
// возврата с подробностями и полем «сказать агенту».
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import type { HumanRequest } from '../api/types';
import { store } from '../store/instance';
import { openWrite } from '../store/writeModal';
import { copyText } from '../util/copy';
import { humanDeadline, humanDecidedBy, humanKindLabel, humanWhere } from '../util/humanRequests';
import { isShowcase } from '../util/showcase';
import { errandLine } from '../util/owner_channel';
import ErrandHands from './ErrandHands.vue';
import ErrandReturned from './ErrandReturned.vue';
import HumanShotModal from './HumanShotModal.vue';

const props = defineProps<{ human: HumanRequest }>();

const busy = ref(false);
const error = ref<string | null>(null);
const shotOpen = ref(false);
const copied = ref(false);
const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  timer = setInterval(() => {
    now.value = Date.now();
  }, 15000); // Ф58-3: «решает агент … N мин» — раз в 15 с
});
onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer);
});

const pending = computed(() => props.human.status === 'pending');
const deadline = computed(() => humanDeadline(props.human, now.value));
const projectName = computed(
  () => store.state.projects.find((p) => p.id === props.human.project_id)?.name ?? `проект ${props.human.project_id}`,
);
const where = computed(() => humanWhere(props.human));
const decidedBy = computed(() => humanDecidedBy(props.human));
const shotSrc = computed(() => `/api/v1/human-requests/${props.human.id}/shot`);
// Витрина: агента владельца нет — строк поручения и возврата нет.
const showcase = isShowcase();
const errandOpen = computed(() => !showcase && pending.value && props.human.errand !== null && props.human.errand !== undefined);
const errandText = computed(() => (errandOpen.value ? errandLine(props.human.errand, now.value) : null));
const returned = computed(() => (!showcase && pending.value && !errandOpen.value ? (props.human.errand_returned ?? null) : null));

async function decide(outcome: 'done' | 'failed' | 'later'): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideHuman(props.human, outcome);
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}

async function letDecide(text: string): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.startErrand({ ref: `human:${props.human.id}` }, text === '' ? { action: 'solve' } : { text });
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}

async function takeBack(): Promise<void> {
  const errand = props.human.errand;
  if (errand === null || errand === undefined) return;
  busy.value = true;
  error.value = null;
  try {
    const result = await store.cancelErrand(errand.id);
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}

async function copyUrl(): Promise<void> {
  if (props.human.url === null) return;
  copied.value = await copyText(props.human.url);
  if (copied.value) setTimeout(() => (copied.value = false), 1500);
}
</script>

<template>
  <div
    class="human-card"
    :data-human-id="human.id"
  >
    <p class="human-meta">
      <span class="human-kind">{{ humanKindLabel(human.kind) }}</span>
      <span class="human-who">{{ projectName }} · {{ human.role }}</span>
      <span
        v-if="pending"
        class="human-deadline"
      >{{ deadline }}</span>
    </p>
    <ErrandHands
      v-if="errandText && human.errand"
      :errand="human.errand"
      test-prefix="human"
    />
    <p
      v-if="human.site"
      class="human-site"
    >
      {{ human.site }}
    </p>
    <p class="human-need">
      {{ human.need }}
    </p>
    <p
      v-if="human.doing"
      class="human-doing"
    >
      {{ human.doing }}
    </p>
    <p
      v-if="human.url"
      class="human-url"
    >
      <span class="human-url-text">{{ human.url }}</span>
      <button
        type="button"
        class="human-copy"
        @click="copyUrl"
      >
        {{ copied ? 'скопировано' : 'копировать' }}
      </button>
    </p>
    <p
      v-if="where"
      class="human-where"
    >
      {{ where }}
    </p>
    <button
      v-if="human.shot"
      type="button"
      class="human-shot"
      aria-label="Открыть скриншот"
      @click="shotOpen = true"
    >
      <img
        :src="shotSrc"
        alt="скриншот"
      >
    </button>
    <p
      v-if="errandText"
      class="human-errand"
      data-test="human-errand"
    >
      {{ errandText }}
      <button
        type="button"
        class="link-button"
        data-test="human-take-back"
        :disabled="busy"
        @click="takeBack"
      >
        Остановить агента
      </button>
    </p>
    <ErrandReturned
      v-else-if="returned"
      class="human-returned"
      test-prefix="human"
      :returned="returned"
      :busy="busy"
      @say="letDecide"
      @talk="openWrite({ kind: 'owner_channel' }, { ref: `human:${human.id}`, title: human.need })"
    />
    <div
      v-if="pending"
      class="human-actions"
    >
      <button
        type="button"
        class="human-done"
        :disabled="busy"
        @click="decide('done')"
      >
        Готово
      </button>
      <button
        type="button"
        class="human-failed"
        :disabled="busy"
        @click="decide('failed')"
      >
        Не вышло
      </button>
      <button
        type="button"
        class="human-later"
        :disabled="busy"
        @click="decide('later')"
      >
        Не сейчас
      </button>
    </div>
    <p
      v-else-if="!pending && decidedBy"
      class="human-decided"
    >
      ответил: {{ decidedBy }}
    </p>
    <p
      v-if="!pending && human.decided_note"
      class="human-decided human-note"
    >
      причина: {{ human.decided_note }}
    </p>
    <p
      v-if="error"
      class="join-error"
      role="alert"
    >
      {{ error }}
    </p>
    <HumanShotModal
      v-if="shotOpen"
      :src="shotSrc"
      :title="human.need"
      @close="shotOpen = false"
    />
  </div>
</template>
