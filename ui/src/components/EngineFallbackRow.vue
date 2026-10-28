<script setup lang="ts">
// Ф83-3: строка «Запасной» блока окна «Движок» (агент или факты): пометка «на запасном: … с ЧЧ:ММ» и кнопка
// «Вернуть на основной» (только владелец, `POST /engine/fallback/{who}/return`), выбор «нет | задать» и тот же выбор
// «движок · модель · усилие» (`PUT /engine/fallback/{who}`); движок основного в запасном недоступен у фактов (Ф85-1: у
// агента — доступен с другой моделью; совпадение целиком — 422 ядра текстом).
import { computed, ref, watch } from 'vue';

import * as api from '../api/client';
import type { EngineName, EngineStatus } from '../api/types';
import { type Choice, choiceLabel, same } from '../util/engine';
import {
  effectiveOf,
  factsFallbackIsOwn,
  fallbackChoiceOf,
  fallbackError,
  fallbackExclude,
  firstFallbackChoice,
  onFallbackLine,
  onFallbackOf,
} from '../util/engineFallback';
import EngineChoice from './EngineChoice.vue';

const props = defineProps<{
  who: 'agent' | 'facts';
  status: EngineStatus | null;
  /** Движок основного выбора этого блока. */
  main: EngineName;
  busy: boolean;
}>();
const emit = defineEmits<{ status: [value: EngineStatus]; notice: [text: string]; busy: [value: boolean] }>();

const current = computed(() => fallbackChoiceOf(props.status, props.who));
const on = computed(() => onFallbackOf(props.status, props.who));
const onLine = computed(() => (on.value === null ? null : onFallbackLine(on.value, effectiveOf(props.status, props.who))));
const inherited = computed(() => props.who === 'facts' && current.value !== null && !factsFallbackIsOwn(props.status));

const enabled = ref(false);
const draft = ref<Choice | null>(null);
const local = ref(false);

function sync(): void {
  enabled.value = current.value !== null;
  draft.value = current.value === null ? null : { ...current.value };
}
watch(() => props.status, sync, { immediate: true });

const changed = computed(() => {
  if (!enabled.value) return current.value !== null && !inherited.value;
  return draft.value !== null && !same(draft.value, current.value);
});

function setEnabled(value: boolean): void {
  enabled.value = value;
  if (value && draft.value === null) draft.value = firstFallbackChoice(props.status, props.main, props.who);
}

async function guarded(fn: () => Promise<EngineStatus>, ok: string): Promise<void> {
  if (local.value || props.busy) return;
  local.value = true;
  emit('busy', true);
  try {
    emit('status', await fn());
    emit('notice', ok);
  } catch (err) {
    emit('notice', fallbackError(err));
  } finally {
    local.value = false;
    emit('busy', false);
  }
}

async function apply(): Promise<void> {
  if (!changed.value) return;
  const choice = enabled.value ? draft.value : null;
  const what = props.who === 'agent' ? 'агента' : 'фактов';
  await guarded(
    () =>
      choice === null
        ? api.putEngineFallback(props.who, null)
        : api.putEngineFallback(props.who, choice.engine, { model: choice.model, effort: choice.effort }),
    choice === null ? `Запасной ${what} снят.` : `Запасной ${what}: ${choiceLabel(choice)}.`,
  );
}

async function giveBack(): Promise<void> {
  await guarded(() => api.returnEngineFallback(props.who), 'Вернул на основной.');
}
</script>

<template>
  <div
    class="engine-fallback"
    :data-fallback="who"
  >
    <p
      v-if="onLine !== null"
      class="engine-onfallback"
      role="status"
    >
      {{ onLine }}
      <button
        type="button"
        class="engine-return"
        :disabled="busy || local"
        @click="giveBack"
      >
        Вернуть на основной
      </button>
    </p>
    <div class="engine-fallback-head">
      <strong>Запасной</strong>
      <div class="engine-mode">
        <label>
          <input
            type="radio"
            :name="`engine-fallback-${who}`"
            value="off"
            :checked="!enabled"
            :disabled="busy || local"
            @change="setEnabled(false)"
          >
          нет
        </label>
        <label>
          <input
            type="radio"
            :name="`engine-fallback-${who}`"
            value="on"
            :checked="enabled"
            :disabled="busy || local"
            @change="setEnabled(true)"
          >
          задать
        </label>
      </div>
      <span
        v-if="inherited"
        class="engine-hint"
      >как у агента</span>
    </div>
    <EngineChoice
      v-if="enabled && draft !== null"
      v-model="draft"
      :kind="who"
      :status="status"
      :busy="busy || local"
      :exclude="fallbackExclude(who, main)"
    />
    <button
      type="button"
      class="engine-apply engine-apply-fallback"
      :disabled="busy || local || !changed"
      @click="apply"
    >
      Применить запасной
    </button>
  </div>
</template>
