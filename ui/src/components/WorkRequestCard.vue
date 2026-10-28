<script setup lang="ts">
// Ф9-2 B → Ф78-1, «Запросы» сессий хаба: разрешение («Просит: <инструмент>», вход целиком, причина, «Разрешить» / «Нет» с
// необязательным комментарием) или вопрос сессии (варианты кнопками и «свой ответ» -> `answers`). 409 —
// «запрос протух» и реплика «разрешено, повтори». Вход инструмента и тексты вопросов — только текстом.
import { computed, reactive, ref } from 'vue';

import type { SessionRequest } from '../api/types';
import type { WorkActionResult } from '../store/store';
import { parseQuestions, permissionInputView } from '../util/dispatch';
import { ALWAYS_BUTTON, alwaysHint } from '../util/sessionAlways';
import { onSubmitKey, submitKeyLabel } from '../util/submitKeys';
import RequestTextModal from './RequestTextModal.vue';

// Решение, `retry` (реплика «разрешено, повтори» в очередь сессии) и `dismiss` (убрать протухшую) задаёт хозяин.

const props = defineProps<{
  request: SessionRequest;
  stale?: boolean;
  decide: (body: {
    decision: 'allow' | 'deny';
    answers?: Record<string, string>;
    message?: string;
    always?: boolean;
  }) => Promise<WorkActionResult>;
  retry: () => Promise<WorkActionResult>;
  dismiss: () => void;
  /** Ф41-5: сессия в авто — название выключателя, который придержал запрос («Автопилот придержал: …»). */
  autoTitle?: string | null;
  /** Владелец 12.10.2026: окно сессии — разрешение свёрнуто до двух строк; «развернуть» или «открыть окном». */
  compact?: boolean;
  /** Блок «Ждёт тебя» главной и «Ждёт» телефона: свёрнутое разрешение — одной строкой с кнопками. */
  oneLine?: boolean;
}>();

function dismissStale(): void {
  props.dismiss();
}

const busy = ref(false);
const answerButton = ref<HTMLButtonElement | null>(null);
const keyLabel = submitKeyLabel();
const error = ref<string | null>(null);
const denyComment = ref('');
const retrySent = ref(false);

const isQuestion = computed(() => props.request.kind === 'question');
const inputView = computed(() => permissionInputView(props.request));
// Свёрнутый вид: начало того, на что просят разрешение, — команда, иначе вход инструмента.
const expanded = ref(false);
const showModal = ref(false);
const foldable = computed(() => (props.compact === true || props.oneLine === true) && !isQuestion.value);
const folded = computed(() => foldable.value && !expanded.value);
const line = computed(() => props.oneLine === true && folded.value);
// Ф90-1: окно сессии (compact + oneLine) — «⌄» вместо двух ссылок; «открыть окном» — в развёрнутом виде.
const tight = computed(() => props.compact === true && props.oneLine === true);
const preview = computed(() => inputView.value.command ?? inputView.value.json ?? inputView.value.description ?? '');
const headText = computed(() =>
  props.autoTitle ? `Автопилот придержал: ${props.autoTitle}` : `Просит: ${props.request.tool ?? '?'}`,
);
const questions = computed(() => parseQuestions(props.request.input));
// Ф99-7.2: ключ «всегда» карточки называет ядро (у кнопок этапа 1, вопросов, хаба и прочих — `null`).
const alwaysKey = computed(() => {
  const key = props.request.always;
  return !isQuestion.value && typeof key === 'string' && key !== '' ? key : null;
});

// Выбранные варианты по индексу вопроса и «свой ответ».
const picked = reactive<Record<number, string[]>>({});
const custom = reactive<Record<number, string>>({});

function toggleOption(qi: number, label: string, multi: boolean): void {
  const list = picked[qi] ?? [];
  if (multi) {
    picked[qi] = list.includes(label) ? list.filter((x) => x !== label) : [...list, label];
  } else {
    picked[qi] = list.includes(label) ? [] : [label];
  }
}

function answerOf(qi: number): string {
  const own = (custom[qi] ?? '').trim();
  if (own !== '') return own;
  return (picked[qi] ?? []).join(', ');
}

const answers = computed<Record<string, string>>(() => {
  const result: Record<string, string> = {};
  questions.value.forEach((q, qi) => {
    const a = answerOf(qi);
    if (a !== '') result[q.question] = a;
  });
  return result;
});

const canAnswer = computed(
  () => questions.value.length > 0 && Object.keys(answers.value).length === questions.value.length,
);

async function submitDecision(decision: 'allow' | 'deny', always = false): Promise<void> {
  busy.value = true;
  error.value = null;
  const body: { decision: 'allow' | 'deny'; answers?: Record<string, string>; message?: string; always?: boolean } = {
    decision,
  };
  if (decision === 'allow' && isQuestion.value) body.answers = { ...answers.value };
  if (decision === 'allow' && always) body.always = true;
  const comment = denyComment.value.trim();
  if (decision === 'deny' && comment !== '') body.message = comment;
  try {
    const result = await props.decide(body);
    if (result === 'error') error.value = 'Не отправилось — попробуй ещё раз.';
  } finally {
    busy.value = false;
  }
}

async function sayRetry(): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await props.retry();
    if (result === 'ok') {
      retrySent.value = true;
      dismissStale();
    } else {
      error.value = 'Реплика не ушла: сессия уже не идёт.';
    }
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="work-request"
    :class="{ 'work-request--stale': stale, 'work-request--line': line }"
    :data-request-id="request.id"
  >
    <p class="work-request-head">
      <strong
        v-if="autoTitle"
        class="work-request-auto"
      >Автопилот придержал: {{ autoTitle }}</strong>
      <strong v-else-if="isQuestion">Сессия спрашивает</strong>
      <strong v-else>Просит: {{ request.tool ?? '?' }}</strong>
      <span
        v-if="stale"
        class="work-request-stale"
      >запрос протух</span>
    </p>

    <template v-if="folded">
      <pre
        class="work-request-command work-request-preview"
        :class="{ 'work-request-preview--one': line }"
        data-test="request-preview"
      >{{ preview }}</pre>
    </template>
    <template v-else-if="!isQuestion">
      <pre
        v-if="inputView.command !== null"
        class="work-request-command"
      >{{ inputView.command }}</pre>
      <p
        v-if="inputView.description"
        class="work-request-description"
      >
        {{ inputView.description }}
      </p>
      <details
        v-if="inputView.json !== null"
        class="work-request-json"
        :open="inputView.command === null && request.input_truncated"
      >
        <summary>вход инструмента</summary>
        <pre>{{ inputView.json }}</pre>
      </details>
    </template>

    <div
      v-else
      class="work-questions"
    >
      <fieldset
        v-for="(q, qi) in questions"
        :key="qi"
        class="work-question"
        :disabled="busy || stale"
      >
        <legend>{{ q.header ? `${q.header}: ` : '' }}{{ q.question }}</legend>
        <div class="work-question-options">
          <button
            v-for="opt in q.options"
            :key="opt.label"
            type="button"
            class="work-question-option"
            :class="{ 'work-question-option--picked': (picked[qi] ?? []).includes(opt.label) }"
            :aria-pressed="(picked[qi] ?? []).includes(opt.label)"
            :title="opt.description ?? undefined"
            @click="toggleOption(qi, opt.label, q.multiSelect)"
          >
            {{ opt.label }}
          </button>
        </div>
        <input
          v-model="custom[qi]"
          type="text"
          class="work-question-custom"
          placeholder="свой ответ"
          aria-label="Свой ответ"
          @keydown="onSubmitKey($event, answerButton)"
        >
      </fieldset>
      <p
        v-if="questions.length === 0"
        class="work-request-description"
      >
        Вопрос без вариантов — ответь репликой ниже.
      </p>
    </div>

    <p
      v-if="foldable"
      class="work-request-views"
    >
      <button
        type="button"
        class="link-button work-request-expand"
        :aria-expanded="expanded"
        :aria-label="expanded ? 'свернуть' : 'развернуть'"
        :title="expanded ? 'свернуть' : 'развернуть'"
        @click="expanded = !expanded"
      >
        {{ tight ? (expanded ? '⌃' : '⌄') : expanded ? 'свернуть' : 'развернуть' }}
      </button>
      <button
        v-if="!tight || expanded"
        type="button"
        class="link-button work-request-open"
        @click="showModal = true"
      >
        открыть окном
      </button>
    </p>
    <p
      v-if="request.input_truncated && !folded"
      class="work-request-truncated"
    >
      показано не всё
    </p>
    <p
      v-if="request.reason && !folded"
      class="work-request-reason"
    >
      причина: {{ request.reason }}
    </p>

    <div
      v-if="stale"
      class="work-request-actions"
    >
      <button
        type="button"
        :disabled="busy || retrySent"
        @click="sayRetry"
      >
        Написать: разрешено, повтори
      </button>
      <button
        type="button"
        class="link-button"
        @click="dismissStale"
      >
        убрать
      </button>
    </div>
    <div
      v-else
      class="work-request-actions"
    >
      <button
        v-if="isQuestion"
        ref="answerButton"
        type="button"
        class="work-allow"
        :title="keyLabel"
        :disabled="busy || !canAnswer"
        @click="submitDecision('allow')"
      >
        Ответить
      </button>
      <button
        v-else
        type="button"
        class="work-allow"
        :disabled="busy"
        @click="submitDecision('allow')"
      >
        Разрешить
      </button>
      <button
        v-if="alwaysKey !== null"
        type="button"
        class="work-allow-always"
        data-test="request-always"
        :title="alwaysHint(alwaysKey)"
        :disabled="busy"
        @click="submitDecision('allow', true)"
      >
        {{ line ? 'Всегда' : ALWAYS_BUTTON }}
      </button>
      <button
        type="button"
        class="work-deny"
        :disabled="busy"
        @click="submitDecision('deny')"
      >
        Нет
      </button>
      <input
        v-if="!line"
        v-model="denyComment"
        type="text"
        class="work-deny-comment"
        placeholder="комментарий к «Нет» (необязательно)"
        aria-label="Комментарий к отказу"
        maxlength="2000"
      >
    </div>
    <p
      v-if="error"
      class="work-error"
      role="alert"
    >
      {{ error }}
    </p>
    <RequestTextModal
      v-if="showModal"
      :title="headText"
      :view="inputView"
      :reason="request.reason ?? null"
      :truncated="request.input_truncated === true"
      @close="showModal = false"
    />
  </div>
</template>
