<script setup lang="ts">
// Ф60-1C: «⋯ → Интервью…» окна сессии хаба — режим подсказок и язык собеседника. Если интервью уже идёт в другой
// сессии — вместо формы ссылка на её окно; если в этой — подсказка про панель. Телефон — формы нет.
import { computed, onMounted, ref } from 'vue';

import type { HubSession } from '../api/types';
import { interviewOpen, interviewUi, loadInterviewCurrent, startInterview } from '../store/interview';
import { store } from '../store/instance';

const props = defineProps<{ session: HubSession }>();
const emit = defineEmits<{ done: []; cancel: [] }>();

const mode = ref<'live' | 'button'>('live');
const lang = ref<'ru-RU' | 'en-US'>('ru-RU');
const busy = ref(false);
const error = ref<string | null>(null);

onMounted(() => {
  void loadInterviewCurrent();
});

const running = computed(() => (interviewOpen(interviewUi.interview) ? interviewUi.interview : null));
const here = computed(() => running.value !== null && running.value.session_id === props.session.id);
const elsewhere = computed(() => (running.value !== null && !here.value ? running.value.session_id : null));

function openOther(): void {
  const other = elsewhere.value;
  if (other === null) return;
  // Так же открывается окно сессии в других местах интерфейса (`WriteModal`): раскрыть и перечитать запись.
  store.state.sessions.expanded[other] = true;
  void store.refreshSession(other);
  emit('cancel');
}

async function submit(): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const res = await startInterview(props.session.id, mode.value, lang.value);
    if (res.ok) emit('done');
    else error.value = res.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    v-if="here"
    class="work-new-task session-interview-form"
  >
    <p class="session-interview-here">
      Интервью уже идёт в этой сессии — панель над полем ввода.
    </p>
    <button
      type="button"
      class="link-button"
      @click="emit('cancel')"
    >
      Закрыть
    </button>
  </div>
  <div
    v-else-if="elsewhere !== null"
    class="work-new-task session-interview-form"
  >
    <p class="session-interview-elsewhere">
      идёт в сессии
      <button
        type="button"
        class="link-button session-interview-link"
        @click="openOther"
      >
        #{{ elsewhere }}
      </button>
    </p>
    <button
      type="button"
      class="link-button"
      @click="emit('cancel')"
    >
      Закрыть
    </button>
  </div>
  <form
    v-else
    class="work-new-task session-interview-form"
    aria-label="Интервью"
    @submit.prevent="submit"
  >
    <label class="work-field">
      <span>Подсказки</span>
      <select v-model="mode">
        <option value="live">
          Подсказки по ходу
        </option>
        <option value="button">
          Только по кнопке
        </option>
      </select>
    </label>
    <label class="work-field">
      <span>Язык собеседника</span>
      <select v-model="lang">
        <option value="ru-RU">
          русский
        </option>
        <option value="en-US">
          английский
        </option>
      </select>
    </label>
    <div class="work-actions">
      <button
        type="submit"
        class="session-interview-start"
        :disabled="busy"
      >
        Начать
      </button>
      <button
        type="button"
        class="link-button"
        @click="emit('cancel')"
      >
        Отмена
      </button>
    </div>
    <p
      v-if="error"
      class="work-error"
      role="alert"
    >
      {{ error }}
    </p>
  </form>
</template>
