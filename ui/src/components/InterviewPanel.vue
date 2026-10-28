<script setup lang="ts">
// Ф60-1C (`docs/architecture.md`): панель помощника на интервью между лентой и полем ввода
// окна сессии хаба. Строки расшифровки, «Подскажи», «Пауза / Продолжить», «Закончить». Тексты строк, причины и
// предупреждения — чужой ввод: только текстовая интерполяция. Телефон — панели нет.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import type { HubSession } from '../api/types';
import {
  endInterview,
  hintInterview,
  interviewUi,
  pauseInterview,
  resumeInterview,
  type InterviewActionResult,
} from '../store/interview';
import { copyText } from '../util/copy';
import { INTERVIEW_END_CONFIRM, LISTEN_ENABLE_COMMAND, clock, interviewMinutes, modeLabel, whoLabel } from '../util/interview';
import { isPhone } from '../util/layout';

const props = defineProps<{ session: HubSession }>();

const interview = computed(() => (interviewUi.interview?.session_id === props.session.id ? interviewUi.interview : null));
const visible = computed(() => interview.value !== null && !isPhone() && !dismissedEnded.value);
const open = computed(() => interview.value !== null && interview.value.status !== 'ended');
const paused = computed(() => interview.value?.status === 'paused');
const dismissedEnded = ref(false);
watch(
  () => interview.value?.id,
  () => (dismissedEnded.value = false),
);

const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  timer = setInterval(() => (now.value = Date.now()), 10000);
});
onBeforeUnmount(() => {
  if (timer !== null) clearInterval(timer);
});
const minutes = computed(() => (interview.value === null ? 0 : interviewMinutes(interview.value.started_ts, now.value)));

const busy = ref(false);
const error = ref<string | null>(null);
const note = ref<string | null>(null);
const copied = ref(false);
const confirmingEnd = ref(false);

async function act(fn: () => Promise<InterviewActionResult>): Promise<InterviewActionResult> {
  busy.value = true;
  error.value = null;
  note.value = null;
  try {
    const res = await fn();
    if (!res.ok) error.value = res.message;
    return res;
  } finally {
    busy.value = false;
  }
}

async function hint(): Promise<void> {
  const cur = interview.value;
  if (cur === null) return;
  const res = await act(() => hintInterview(cur.id));
  if (res.ok) note.value = res.queued === true ? 'уже запрошено' : 'подсказка запрошена';
}

async function togglePause(): Promise<void> {
  const cur = interview.value;
  if (cur === null) return;
  await act(() => (cur.status === 'paused' ? resumeInterview(cur.id) : pauseInterview(cur.id)));
}

async function end(): Promise<void> {
  const cur = interview.value;
  if (cur === null) return;
  const res = await act(() => endInterview(cur.id));
  if (res.ok) confirmingEnd.value = false;
}

async function copyCommand(): Promise<void> {
  if (await copyText(LISTEN_ENABLE_COMMAND)) {
    copied.value = true;
    setTimeout(() => (copied.value = false), 2000);
  }
}

// Лента: видны ~8 строк, остальное листается; автопрокрутка вниз, пока владелец у низа.
const listEl = ref<HTMLElement | null>(null);
const atBottom = ref(true);
function onScroll(): void {
  const el = listEl.value;
  if (el === null) return;
  atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
}
watch(
  () => interviewUi.lines.length,
  async () => {
    if (!atBottom.value) return;
    await nextTick();
    const el = listEl.value;
    if (el !== null) el.scrollTop = el.scrollHeight;
  },
);
</script>

<template>
  <section
    v-if="visible && interview !== null"
    class="interview-panel"
    :class="{ 'interview-panel--ended': !open }"
    aria-label="Интервью"
  >
    <h4 class="interview-head">
      <template v-if="open">
        Интервью · {{ minutes }} мин · {{ modeLabel(interview.mode) }}<template v-if="paused"> · пауза</template>
      </template>
      <template v-else>
        Интервью окончено
      </template>
    </h4>

    <ul
      ref="listEl"
      class="interview-lines"
      aria-label="Расшифровка"
      @scroll="onScroll"
    >
      <li
        v-for="line in interviewUi.lines"
        :key="line.id"
        class="interview-line"
        :class="line.who === 'me' ? 'interview-line--me' : 'interview-line--them'"
      >
        <span class="interview-line-time">{{ clock(line.start_ms) }}</span>
        <span class="interview-line-who">{{ whoLabel(line.who) }}:</span>
        <span class="interview-line-text">{{ line.text }}</span>
      </li>
      <li
        v-if="interviewUi.lines.length === 0"
        class="interview-empty"
      >
        {{ open ? 'речи пока нет' : 'строк не было' }}
      </li>
    </ul>

    <p
      v-if="interview.warn"
      class="interview-warn"
      role="status"
    >
      {{ interview.warn }}
    </p>
    <p
      v-if="interview.reason"
      class="interview-reason"
    >
      {{ open ? 'Пауза' : 'Конец' }}: {{ interview.reason }}
    </p>
    <p
      v-if="open && interview.wait_reason"
      class="interview-wait"
    >
      подача ждёт: {{ interview.wait_reason }}
    </p>
    <div
      v-if="open && !interviewUi.listenerAlive"
      class="interview-listener"
      role="alert"
    >
      <span>служба записи не запущена</span>
      <code class="interview-command">{{ LISTEN_ENABLE_COMMAND }}</code>
      <button
        type="button"
        class="link-button interview-copy"
        @click="copyCommand"
      >
        {{ copied ? 'скопировано' : 'копировать' }}
      </button>
    </div>

    <div
      v-if="open"
      class="work-actions interview-actions"
    >
      <button
        type="button"
        class="interview-hint"
        :disabled="busy || paused"
        @click="hint"
      >
        Подскажи
      </button>
      <button
        type="button"
        class="interview-pause"
        :disabled="busy"
        @click="togglePause"
      >
        {{ paused ? 'Продолжить' : 'Пауза' }}
      </button>
      <button
        v-if="!confirmingEnd"
        type="button"
        class="interview-end"
        :disabled="busy"
        @click="confirmingEnd = true"
      >
        Закончить
      </button>
      <span
        v-if="note"
        class="interview-note"
        role="status"
      >{{ note }}</span>
    </div>
    <div
      v-if="open && confirmingEnd"
      class="interview-end-confirm"
    >
      <p>{{ INTERVIEW_END_CONFIRM }}</p>
      <div class="work-actions">
        <button
          type="button"
          class="interview-end-yes"
          :disabled="busy"
          @click="end"
        >
          Да, закончить
        </button>
        <button
          type="button"
          class="link-button interview-end-no"
          @click="confirmingEnd = false"
        >
          Не заканчивать
        </button>
      </div>
    </div>
    <button
      v-if="!open"
      type="button"
      class="link-button interview-dismiss"
      @click="dismissedEnded = true"
    >
      Скрыть
    </button>
    <p
      v-if="error"
      class="work-error interview-error"
      role="alert"
    >
      {{ error }}
    </p>
  </section>
</template>
