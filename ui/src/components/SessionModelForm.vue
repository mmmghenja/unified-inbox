<script setup lang="ts">
// Ф41-7а (интерфейс, `docs/architecture.md`): «Модель и усилие…» из «⋯» окна сессии — те же поля
// и подсказки, что в форме «Новая сессия». Предупреждение: кэш сбросится — следующий ход прочитает весь контекст
// заново (размер — из `usage`); «применится со следующего входа». Движок на месте не меняется.
import { computed, ref, watch } from 'vue';

import type { HubSession } from '../api/types';
import { store } from '../store/instance';
import { sessionEngineChoices } from '../util/engineProfiles';
import {
  fallbackBody,
  type FallbackDraft,
  projectFallbackLine,
  sameDraft,
  sessionFallbackDraft,
} from '../util/engineFallback';
import { effortChoices, effortForEngine, modelDefaultEffort, sessionModelHints } from '../util/sessions';

const props = defineProps<{ session: HubSession }>();
const emit = defineEmits<{ done: []; cancel: [] }>();

const model = ref(props.session.model ?? '');
const effort = ref(props.session.effort ?? '');
const busy = ref(false);
// Ф83-3: «Запасной» — как у проекта / свой / нет; запасной — другой движок, не тот, на котором сессия.
const original = sessionFallbackDraft(props.session);
const fallback = ref<FallbackDraft>({ ...original });
const fbEngines = computed(() => sessionEngineChoices().filter((p) => p.name !== props.session.engine));
const fbEfforts = computed(() =>
  fallback.value.engine === '' ? [] : effortChoices(fallback.value.engine, fallback.value.effort, fallback.value.model, true),
);
const projectLine = computed(() =>
  projectFallbackLine(store.state.projects.find((p) => p.id === props.session.project_id)?.session_fallback),
);
watch(
  () => fallback.value.engine,
  (engine) => {
    if (engine !== '' && engine !== original.engine) {
      fallback.value.model = '';
      fallback.value.effort = '';
    }
  },
);
const error = ref<string | null>(null);

const hints = computed(() => sessionModelHints(props.session.engine));
const efforts = computed(() =>
  effortChoices(props.session.engine, effort.value, model.value, model.value.trim() === (props.session.model ?? '')),
);
const defaultEffort = computed(() => modelDefaultEffort(props.session.engine, model.value));
// Ф74-6: смена модели — усилие не из её списка сбрасывается в «по умолчанию».
watch(model, (value) => {
  if (value.trim() !== (props.session.model ?? '')) effort.value = effortForEngine(props.session.engine, effort.value, value);
});
const listId = computed(() => `session-model-hints-${props.session.id}`);
// Подсказки модели запасного — список моделей его движка (как у основной модели).
const fbHints = computed(() => (fallback.value.engine === '' ? [] : sessionModelHints(fallback.value.engine)));
const fbListId = computed(() => `session-fallback-model-hints-${props.session.id}`);
const contextTokens = computed(() => props.session.usage?.context_tokens ?? null);
const warning = computed(() => {
  const tail = contextTokens.value === null ? '' : ` (сейчас ${Math.max(1, Math.round(contextTokens.value / 1000))} тыс.)`;
  return `кэш сбросится — следующий ход прочитает весь контекст заново${tail}`;
});

async function save(): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const nextModel = model.value.trim() === '' ? null : model.value.trim();
    const nextEffort = effort.value !== '' ? effort.value : null;
    const fbChanged = !sameDraft(fallback.value, original);
    const body = fallbackBody(fallback.value);
    if (fbChanged && body === undefined) {
      error.value = 'Запасной: выбери движок.';
      return;
    }
    if (nextModel !== (props.session.model ?? null) || nextEffort !== (props.session.effort ?? null) || !fbChanged) {
      const result = await store.setSessionModel(props.session.id, nextModel, nextEffort);
      if (!result.ok) {
        error.value = result.message;
        return;
      }
    }
    if (fbChanged && body !== undefined) {
      const result = await store.setSessionFallback(props.session.id, body);
      if (!result.ok) {
        error.value = result.message;
        return;
      }
    }
    emit('done');
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <form
    class="work-new-task session-model-form"
    aria-label="Модель и усилие"
    @submit.prevent="save"
  >
    <label class="work-field">
      <span>Модель</span>
      <input
        v-model="model"
        type="text"
        class="session-model-input"
        :list="listId"
        maxlength="64"
        autocomplete="off"
      >
      <datalist :id="listId">
        <option
          v-for="hint in hints"
          :key="hint"
          :value="hint"
        />
      </datalist>
    </label>
    <label class="work-field">
      <span>Усилие</span>
      <select
        v-model="effort"
        class="session-effort-input"
      >
        <option value="">
          по умолчанию{{ defaultEffort ? ` (${defaultEffort})` : '' }}
        </option>
        <option
          v-for="e in efforts"
          :key="e"
          :value="e"
        >
          {{ e }}
        </option>
      </select>
    </label>
    <fieldset class="session-fallback">
      <legend>Запасной</legend>
      <label>
        <input
          v-model="fallback.mode"
          type="radio"
          value="inherit"
          name="session-fallback-mode"
        >
        как у проекта ({{ projectLine }})
      </label>
      <label>
        <input
          v-model="fallback.mode"
          type="radio"
          value="own"
          name="session-fallback-mode"
        >
        свой
      </label>
      <label>
        <input
          v-model="fallback.mode"
          type="radio"
          value="none"
          name="session-fallback-mode"
        >
        нет
      </label>
      <template v-if="fallback.mode === 'own'">
        <label class="work-field">
          <span>Движок запасного</span>
          <select
            v-model="fallback.engine"
            class="session-fallback-engine"
          >
            <option value="">
              выбери
            </option>
            <option
              v-for="p in fbEngines"
              :key="p.name"
              :value="p.name"
            >
              {{ p.label }}
            </option>
          </select>
        </label>
        <label class="work-field">
          <span>Модель запасного</span>
          <input
            v-model="fallback.model"
            type="text"
            class="session-fallback-model"
            :list="fbListId"
            maxlength="64"
            autocomplete="off"
          >
          <datalist :id="fbListId">
            <option
              v-for="hint in fbHints"
              :key="hint"
              :value="hint"
            />
          </datalist>
        </label>
        <label
          v-if="fbEfforts.length > 0"
          class="work-field"
        >
          <span>Усилие запасного</span>
          <select
            v-model="fallback.effort"
            class="session-fallback-effort"
          >
            <option value="">
              по умолчанию
            </option>
            <option
              v-for="e in fbEfforts"
              :key="e"
              :value="e"
            >
              {{ e }}
            </option>
          </select>
        </label>
      </template>
    </fieldset>
    <p class="session-model-warning">
      {{ warning }}; применится со следующего входа.
    </p>
    <div class="work-actions">
      <button
        type="submit"
        class="session-model-save"
        :disabled="busy"
      >
        Сохранить
      </button>
      <button
        type="button"
        class="link-button"
        @click="emit('cancel')"
      >
        отмена
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
