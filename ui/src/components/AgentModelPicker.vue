<script setup lang="ts">
// Ф75-1 (docs/architecture.md.3): строка «Модель: <модель · уровень> ▾» над разговором
// «Мой агент» (компьютер и телефон). Клик — форма: модель из списка OpenCode службы (свободный ввод тоже), уровень из
// её вариантов; «Сохранить» — `PUT /owner_channel/agent-settings`, «По умолчанию» — снять выбор. Действует со следующего
// хода агента, перезапуска не нужно.
import { computed, onMounted, ref, watch } from 'vue';

import { getOwnerAgentSettings, getOwnerProjectModel, putOwnerAgentSettings, putOwnerProjectModel } from '../api/client';
import type { OwnerAgentSettings, OwnerProjectModel } from '../api/types';
import { ENGINE_LABEL } from '../util/engine';
import {
  agentModelCurrent,
  agentModelInputError,
  agentModelLabel,
  agentModelOption,
  agentSettingsError,
  agentVariantChoices,
  agentVariantFor,
} from '../util/agentModel';

// Ф76-4б: `projectId` — своя модель проекта (`PUT /owner_channel/agent-settings/projects/{id}`; null снимает — «как общая»).
const props = defineProps<{ projectId?: number }>();
const general = ref<OwnerAgentSettings | null>(null);
const projectModel = ref<OwnerProjectModel | null>(null);
// Для проекта: выбор — его замена, «по умолчанию» — действующая общая модель.
const settings = computed<OwnerAgentSettings | null>(() => {
  const g = general.value;
  if (g === null || props.projectId === undefined) return g;
  const pm = projectModel.value ?? { model: null, variant: null };
  const base = agentModelCurrent(g);
  return { ...g, model: pm.model, variant: pm.variant, default: { model: base.model, variant: base.variant } };
});
const loadError = ref<string | null>(null);
const open = ref(false);
const model = ref('');
const variant = ref('');
const busy = ref(false);
const error = ref<string | null>(null);
const saved = ref<string | null>(null);

const models = computed(() => settings.value?.models ?? []);
const label = computed(() => {
  const s = settings.value;
  if (s === null) return '…';
  if (props.projectId === undefined) return agentModelLabel(s);
  const cur = agentModelCurrent(s);
  const head = cur.variant === null || cur.variant === '' ? cur.model : `${cur.model} · ${cur.variant}`;
  return cur.chosen ? head : `как общая (${head})`;
});
const choices = computed(() => agentVariantChoices(models.value, model.value));
// Ф78-2c: движок агента — из ответа ядра (старое ядро не присылает — OpenCode).
const engine = computed(() => general.value?.engine ?? 'opencode');
const isOpencode = computed(() => engine.value === 'opencode');
const engineName = computed(() => ENGINE_LABEL[engine.value] ?? engine.value);
const inputError = computed(() => agentModelInputError(model.value, engine.value));

async function load(): Promise<void> {
  try {
    general.value = await getOwnerAgentSettings();
    projectModel.value = props.projectId === undefined ? null : await getOwnerProjectModel(props.projectId);
    loadError.value = null;
  } catch {
    loadError.value = 'модель агента не загрузилась';
  }
}

function toggle(): void {
  if (settings.value === null) return;
  open.value = !open.value;
  if (!open.value) return;
  const cur = agentModelCurrent(settings.value);
  model.value = cur.model;
  variant.value = cur.variant ?? '';
  error.value = null;
  saved.value = null;
}

watch(model, (value) => {
  variant.value = agentVariantFor(models.value, value, variant.value);
});

async function write(body: { model: string | null; variant: string | null }): Promise<void> {
  busy.value = true;
  error.value = null;
  saved.value = null;
  try {
    if (props.projectId === undefined) {
      general.value = await putOwnerAgentSettings(body);
    } else {
      await putOwnerProjectModel(props.projectId, body);
      projectModel.value = { model: body.model, variant: body.model === null ? null : body.variant };
    }
    open.value = false;
    saved.value = `${label.value} — со следующего хода агента`;
  } catch (err) {
    error.value = agentSettingsError(err);
  } finally {
    busy.value = false;
  }
}

async function save(): Promise<void> {
  if (inputError.value !== null) return;
  const m = model.value.trim();
  const v = variant.value.trim();
  await write({ model: m === '' ? null : m, variant: m === '' || v === '' ? null : v });
}

async function reset(): Promise<void> {
  await write({ model: null, variant: null });
}

onMounted(load);
watch(() => props.projectId, load);
</script>

<template>
  <div
    class="agent-model"
    data-test="agent-model"
  >
    <button
      type="button"
      class="link-button agent-model-line"
      :disabled="settings === null"
      :aria-expanded="open"
      data-test="agent-model-line"
      @click="toggle"
    >
      Модель<template v-if="!isOpencode"> ({{ engineName }})</template>: {{ label }} ▾
    </button>
    <span
      v-if="saved !== null"
      class="agent-model-saved"
      data-test="agent-model-saved"
    >сохранено: {{ saved }}</span>
    <span
      v-if="loadError !== null"
      class="work-error"
    >{{ loadError }}</span>
    <form
      v-if="open && settings !== null"
      class="work-new-task agent-model-form"
      aria-label="Модель агента"
      data-test="agent-model-form"
      @submit.prevent="save"
    >
      <label class="work-field">
        <span>Модель</span>
        <input
          v-model="model"
          type="text"
          class="agent-model-input"
          list="agent-model-list"
          maxlength="200"
          autocomplete="off"
          :placeholder="isOpencode ? 'провайдер/модель' : 'модель'"
          data-test="agent-model-input"
        >
        <datalist id="agent-model-list">
          <option
            v-for="m in models"
            :key="m.id"
            :value="m.id"
          >{{ agentModelOption(m) }}</option>
        </datalist>
      </label>
      <label class="work-field">
        <span>Уровень</span>
        <select
          v-if="choices !== null"
          v-model="variant"
          class="agent-variant-input"
          data-test="agent-variant-select"
        >
          <option value="">по умолчанию</option>
          <option
            v-for="v in choices"
            :key="v"
            :value="v"
          >{{ v }}</option>
        </select>
        <input
          v-else
          v-model="variant"
          type="text"
          class="agent-variant-input"
          maxlength="40"
          autocomplete="off"
          placeholder="по умолчанию"
          data-test="agent-variant-input"
        >
      </label>
      <p
        v-if="models.length === 0"
        class="agent-model-hint"
      >
        Список моделей служба агента ещё не прислала — впиши {{ isOpencode ? '«провайдер/модель»' : 'название модели' }}.
      </p>
      <p
        v-else-if="choices === null && model.trim() !== ''"
        class="agent-model-hint"
      >
        Этой модели нет в списке {{ engineName }}: {{ isOpencode ? 'если её нет и у самого OpenCode, агент ответит на запасной и скажет об этом.' : 'ядро может её не принять.' }}
      </p>
      <p
        v-if="inputError !== null"
        class="work-error"
      >
        {{ inputError }}
      </p>
      <div class="work-actions">
        <button
          type="submit"
          :disabled="busy || inputError !== null"
          data-test="agent-model-save"
        >
          Сохранить
        </button>
        <button
          type="button"
          class="link-button"
          :disabled="busy || settings.model === null"
          data-test="agent-model-reset"
          @click="reset"
        >
          {{ projectId === undefined ? 'по умолчанию' : 'как общая' }} ({{ settings.default.model }}{{ settings.default.variant ? ` · ${settings.default.variant}` : '' }})
        </button>
        <button
          type="button"
          class="link-button"
          @click="open = false"
        >
          отмена
        </button>
      </div>
      <p
        v-if="error !== null"
        class="work-error"
        role="alert"
        data-test="agent-model-error"
      >
        {{ error }}
      </p>
    </form>
  </div>
</template>
