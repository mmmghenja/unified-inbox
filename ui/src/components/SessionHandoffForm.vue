<script setup lang="ts">
// Ф41-7б (интерфейс, `docs/architecture.md`): «Новая с передачей дел…» из «⋯» окна сессии:
// движок, модель, усилие (подставлены от прежней), режим; пояснение; «Передать». После ответа столбец на доске
// меняется на новую сессию (место в раскладке то же) — это делает стор.
import { computed, ref, watch } from 'vue';

import type { EngineName, HandoffInput, HubSession } from '../api/types';
import { store } from '../store/instance';
import { sessionEngineChoices } from '../util/engineProfiles';
import { effortChoices, effortForEngine, modelDefaultEffort, sessionModelHints } from '../util/sessions';

const props = defineProps<{ session: HubSession }>();
const emit = defineEmits<{ done: []; cancel: [] }>();

const engine = ref<EngineName>(props.session.engine);
const model = ref(props.session.model ?? '');
const effort = ref(props.session.effort ?? '');
const mode = ref<'manual' | 'auto'>(props.session.mode === 'auto' ? 'auto' : 'manual');
const subdir = ref(props.session.subdir ?? '');
const busy = ref(false);
const error = ref<string | null>(null);

// Ф74-1: движки и подсказки — из профилей `/status` (старое ядро — запасные значения).
const engineChoices = computed(() => sessionEngineChoices());
const hints = computed(() => sessionModelHints(engine.value));
const efforts = computed(() => effortChoices(engine.value, effort.value, model.value));
const defaultEffort = computed(() => modelDefaultEffort(engine.value, model.value));
// Смена движка: усилие, которого нет в его списке, сбрасывается в «по умолчанию».
watch(engine, (value) => {
  effort.value = effortForEngine(value, effort.value, model.value);
});
// Ф74-6: смена модели — усилие не из её списка сбрасывается в «по умолчанию».
watch(model, (value) => {
  effort.value = effortForEngine(engine.value, effort.value, value);
});
const listId = computed(() => `session-handoff-models-${props.session.id}`);

async function submit(): Promise<void> {
  busy.value = true;
  error.value = null;
  const input: HandoffInput = { engine: engine.value, mode: mode.value };
  input.model = model.value.trim() === '' ? null : model.value.trim();
  input.effort = effort.value !== '' ? effort.value : null;
  // Ф50-7: ключ — только если значение изменено (очистка = «» — корень проекта).
  if (subdir.value.trim() !== (props.session.subdir ?? '')) input.subdir = subdir.value.trim();
  try {
    const result = await store.handoffSession(props.session.id, input);
    if (result.ok) emit('done');
    else error.value = result.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <form
    class="work-new-task session-handoff-form"
    aria-label="Новая с передачей дел"
    @submit.prevent="submit"
  >
    <p class="session-handoff-note">
      прежняя сессия закроется; новой уйдут конец разговора и ссылка на полную запись
    </p>
    <label class="work-field">
      <span>Движок</span>
      <select
        v-model="engine"
        class="session-handoff-engine"
      >
        <option
          v-for="p in engineChoices"
          :key="p.name"
          :value="p.name"
        >
          {{ p.label }}
        </option>
      </select>
    </label>
    <label class="work-field">
      <span>Модель</span>
      <input
        v-model="model"
        type="text"
        class="session-handoff-model"
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
        class="session-handoff-effort"
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
    <label class="work-field">
      <span>Папка в проекте</span>
      <input
        v-model="subdir"
        type="text"
        class="session-handoff-subdir"
        maxlength="300"
        autocapitalize="off"
        spellcheck="false"
      >
    </label>
    <label class="work-field">
      <span>Режим</span>
      <select
        v-model="mode"
        class="session-handoff-mode"
      >
        <option value="manual">Ручной</option>
        <option value="auto">Авто</option>
      </select>
    </label>
    <div class="work-actions">
      <button
        type="submit"
        class="session-handoff-submit"
        :disabled="busy"
      >
        Передать
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
