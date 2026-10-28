<script setup lang="ts">
// Ф78-2c: выбор «движок · модель · усилие» — общий для строк «Мой агент» и «Факты о людях» панели движка.
// Четыре движка (недоступные серые, с причиной ядра), модель из списка движка («видит картинки» — значком),
// усилие модели. Черновик живёт у хозяина (`EnginePanel`), здесь — только показ и правка.
import { computed } from 'vue';

import type { EngineName, EngineStatus } from '../api/types';
import {
  afterModel,
  type Choice,
  type ChoiceKind,
  defaultChoice,
  effortsFor,
  ENGINE_LABEL,
  ENGINE_NAMES,
  engineInfo,
  modelsOf,
  modelSeesImages,
  NO_IMAGES_WARN,
  optionDisabled,
  optionReason,
} from '../util/engine';
import { FALLBACK_SAME_ENGINE } from '../util/engineFallback';

const props = defineProps<{
  status: EngineStatus | null;
  modelValue: Choice;
  /** Блок агента: модель без картинок — предупреждение про «Реши» с браузером. */
  warnNoImages?: boolean;
  /** Строка «Факты о людях» берёт модели и умолчания из `facts_*` движка. */
  kind?: ChoiceKind;
  busy?: boolean;
  /** Ф83: движок основного выбора — в запасном недоступен (с пояснением). */
  exclude?: EngineName;
}>();
const emit = defineEmits<{ 'update:modelValue': [value: Choice] }>();

const info = computed(() => engineInfo(props.status, props.modelValue.engine));
const kind = computed<ChoiceKind>(() => props.kind ?? 'agent');
const models = computed(() => modelsOf(info.value, kind.value));
const efforts = computed(() => effortsFor(info.value, props.modelValue.model, kind.value));
const seesImages = computed(() => modelSeesImages(props.status, props.modelValue));
const noImages = computed(() => props.warnNoImages === true && seesImages.value === false);

function off(engine: EngineName): boolean {
  return engine === props.exclude || optionDisabled(props.status, engine);
}

function reason(engine: EngineName): string | null {
  return engine === props.exclude ? FALLBACK_SAME_ENGINE : optionReason(props.status, engine);
}

function pickEngine(engine: EngineName): void {
  if (off(engine) || engine === props.modelValue.engine) return;
  emit('update:modelValue', defaultChoice(props.status, engine, kind.value));
}

function pickModel(event: Event): void {
  emit('update:modelValue', afterModel(props.status, props.modelValue, (event.target as HTMLSelectElement).value, kind.value));
}

function pickEffort(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  emit('update:modelValue', { ...props.modelValue, effort: value === '' ? null : value });
}

function modelLabel(m: { id: string; name: string | null; images: boolean | null }): string {
  const name = m.name !== null && m.name !== '' && m.name !== m.id ? `${m.name} (${m.id})` : m.id;
  return m.images === true ? `${name} 🖼` : name;
}
</script>

<template>
  <div class="engine-choice">
    <ul class="engine-options">
      <li
        v-for="engine in ENGINE_NAMES"
        :key="engine"
        class="engine-option"
        :class="{ 'engine-option--current': engine === modelValue.engine, 'engine-option--off': off(engine) }"
        :data-engine="engine"
      >
        <button
          type="button"
          class="engine-choose"
          :disabled="busy === true || off(engine)"
          :aria-pressed="engine === modelValue.engine"
          @click="pickEngine(engine)"
        >
          {{ engine === modelValue.engine ? `${ENGINE_LABEL[engine]} ✓` : ENGINE_LABEL[engine] }}
        </button>
        <span
          v-if="reason(engine) !== null"
          class="engine-warn engine-reason"
        >{{ reason(engine) }}</span>
      </li>
    </ul>
    <div class="engine-fields">
      <label>
        Модель
        <select
          class="engine-model"
          :value="modelValue.model ?? ''"
          :disabled="busy === true || models.length === 0"
          @change="pickModel"
        >
          <option
            v-if="modelValue.model !== null && models.every((m) => m.id !== modelValue.model)"
            :value="modelValue.model"
          >{{ modelValue.model }}</option>
          <option
            v-for="m in models"
            :key="m.id"
            :value="m.id"
          >{{ modelLabel(m) }}</option>
        </select>
      </label>
      <label v-if="efforts.length > 0">
        Усилие
        <select
          class="engine-effort"
          :value="modelValue.effort ?? ''"
          :disabled="busy === true"
          @change="pickEffort"
        >
          <option value="">по умолчанию</option>
          <option
            v-for="e in efforts"
            :key="e"
            :value="e"
          >{{ e }}</option>
        </select>
      </label>
    </div>
    <p
      v-if="noImages"
      class="engine-warn engine-no-images"
    >
      {{ NO_IMAGES_WARN }}
    </p>
  </div>
</template>
