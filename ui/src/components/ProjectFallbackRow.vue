<script setup lang="ts">
// Ф83-3: «Запасной движок сессий» — умолчание проекта в «⋯ → Раскладка» (`PATCH /projects/{id} {session_fallback}`).
// Сессия без своей настройки в форме «Модель и усилие» берёт его; движок самой сессии запасным не бывает —
// ядро такой переход пропускает. Пишет только владелец (кука).
import { computed, ref, watch } from 'vue';

import * as api from '../api/client';
import type { EngineName, Project } from '../api/types';
import { fallbackError } from '../util/engineFallback';
import { sessionEngineChoices } from '../util/engineProfiles';
import { effortChoices, sessionModelHints } from '../util/sessions';

const props = defineProps<{ project: Project; busy: boolean }>();
const emit = defineEmits<{ saved: []; error: [text: string] }>();

const engine = ref<EngineName | ''>(props.project.session_fallback?.engine ?? '');
const model = ref(props.project.session_fallback?.model ?? '');
const effort = ref(props.project.session_fallback?.effort ?? '');
watch(
  () => props.project.session_fallback,
  (value) => {
    engine.value = value?.engine ?? '';
    model.value = value?.model ?? '';
    effort.value = value?.effort ?? '';
  },
);
const engines = sessionEngineChoices();
// Подсказки модели — список моделей выбранного движка (как в форме «Модель и усилие»).
const hints = computed(() => (engine.value === '' ? [] : sessionModelHints(engine.value)));
const listId = computed(() => `project-fallback-model-hints-${props.project.id}`);
const efforts = computed(() => (engine.value === '' ? [] : effortChoices(engine.value, effort.value, model.value, true)));

async function save(): Promise<void> {
  const body =
    engine.value === ''
      ? null
      : {
          engine: engine.value,
          model: model.value.trim() === '' ? null : model.value.trim(),
          effort: effort.value === '' ? null : effort.value,
        };
  try {
    await api.patchProject(props.project.id, { session_fallback: body });
    emit('saved');
  } catch (err) {
    emit('error', fallbackError(err));
  }
}

function onEngine(event: Event): void {
  engine.value = (event.target as HTMLSelectElement).value as EngineName | '';
  model.value = '';
  effort.value = '';
  void save();
}
</script>

<template>
  <div class="mapping-project-fallback">
    <label>
      Запасной движок сессий:
      <select
        class="mapping-project-fallback-engine"
        :aria-label="`Запасной движок сессий — проект ${project.name ?? project.id}`"
        :value="engine"
        :disabled="busy"
        @change="onEngine"
      >
        <option value="">не задан</option>
        <option
          v-for="p in engines"
          :key="p.name"
          :value="p.name"
        >{{ p.label }}</option>
      </select>
    </label>
    <template v-if="engine !== ''">
      <label>
        модель
        <input
          v-model="model"
          type="text"
          class="mapping-project-fallback-model"
          :list="listId"
          maxlength="64"
          autocomplete="off"
          :disabled="busy"
          @change="save"
        >
        <datalist :id="listId">
          <option
            v-for="hint in hints"
            :key="hint"
            :value="hint"
          />
        </datalist>
      </label>
      <label v-if="efforts.length > 0">
        усилие
        <select
          v-model="effort"
          class="mapping-project-fallback-effort"
          :disabled="busy"
          @change="save"
        >
          <option value="">по умолчанию</option>
          <option
            v-for="e in efforts"
            :key="e"
            :value="e"
          >{{ e }}</option>
        </select>
      </label>
    </template>
    <p class="mapping-hint">
      сессия без своего запасного уходит на него при лимите или пустом балансе: авто — сама, ручная — после «Перейти»
    </p>
  </div>
</template>
