<script setup lang="ts">
// Ф16-1 B → Ф78-2c (`docs/architecture.md`, 12, 13): панель «Движок» — из
// пункта меню «⋯» (ноутбук и телефон). Два выбора «движок · модель · усилие»: «Мой агент» (`PUT /engine`) и
// «Факты о людях» (`PUT /engine/facts`; «как у агента» — `engine: null`); у каждого — строка «Запасной» (Ф83-3,
// `EngineFallbackRow`: пометка «на запасном», «Вернуть на основной»). Подписанные сессии проектов не
// затрагиваются. Смена агента — после подтверждения. С телефона работает так же (Ф80-1 E1); 403 старого ядра —
// текстом ошибки. Esc и нажатие мимо закрывают панель (`usePanel`, Ф80-1 B1).
import { computed, onMounted, ref } from 'vue';

import * as api from '../api/client';
import type { EngineStatus } from '../api/types';
import { store } from '../store/instance';
import {
  agentChoice,
  type Choice,
  confirmAgentText,
  confirmFactsText,
  defaultChoice,
  factsChoice,
  same,
  switchedNotice,
  switchError,
} from '../util/engine';
import { usePanel } from '../util/popup';
import EngineChoice from './EngineChoice.vue';
import EngineFallbackRow from './EngineFallbackRow.vue';

const emit = defineEmits<{ close: [] }>();
const panelRoot = ref<HTMLElement | null>(null);
usePanel(() => panelRoot.value, () => emit('close'));

const status = ref<EngineStatus | null>(null);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);
const busy = ref(false);

const agentDraft = ref<Choice | null>(null);
const factsDraft = ref<Choice | null>(null);
const factsFollow = ref(true);

const agent = computed(() => agentChoice(status.value));
const facts = computed(() => factsChoice(status.value));

/** Черновики — по ответу ядра; факты «своё» без записи начинаются с выбора агента. */
function syncDrafts(): void {
  const a = agentChoice(status.value);
  const f = factsChoice(status.value);
  agentDraft.value = a;
  factsFollow.value = f?.follows ?? true;
  factsDraft.value = f === null ? null : { engine: f.engine, model: f.model, effort: f.effort };
}

async function load(): Promise<void> {
  try {
    status.value = await api.getEngine();
    error.value = null;
    syncDrafts();
  } catch {
    error.value = 'Не удалось загрузить состояние движка.';
  }
}

onMounted(() => {
  void load();
});

const agentChanged = computed(() => agentDraft.value !== null && !same(agentDraft.value, agent.value));
const factsChanged = computed(() => {
  const f = facts.value;
  if (f === null || factsDraft.value === null) return false;
  if (factsFollow.value) return !f.follows;
  return f.follows || !same(factsDraft.value, f);
});

function chooseFactsOwn(): void {
  factsFollow.value = false;
  // Первый раз «своё» — с выбора агента, чтобы не прыгать на умолчание другого движка.
  if (factsDraft.value === null && agent.value !== null) factsDraft.value = { ...agent.value };
}

async function afterWrite(result: EngineStatus): Promise<void> {
  status.value = result;
  syncDrafts();
  try {
    await store.refreshStatus(); // чип в меню показывает новый выбор сразу
  } catch {
    // не обновился статус — чип догонит следующий /status, смена уже состоялась
  }
}

async function applyAgent(): Promise<void> {
  const draft = agentDraft.value;
  if (busy.value || draft === null || !agentChanged.value) return;
  if (!window.confirm(confirmAgentText(draft, facts.value?.follows ?? true))) return;
  busy.value = true;
  notice.value = null;
  try {
    await afterWrite(await api.putEngine(draft.engine, { model: draft.model, effort: draft.effort }));
    notice.value = switchedNotice(draft, 'agent');
  } catch (err) {
    notice.value = switchError(err);
    await load();
  } finally {
    busy.value = false;
  }
}

async function applyFacts(): Promise<void> {
  if (busy.value || !factsChanged.value) return;
  const draft = factsFollow.value ? null : (factsDraft.value ?? defaultChoice(status.value, 'claude'));
  if (!window.confirm(confirmFactsText(draft))) return;
  busy.value = true;
  notice.value = null;
  try {
    const result =
      draft === null
        ? await api.putEngineFacts(null)
        : await api.putEngineFacts(draft.engine, { model: draft.model, effort: draft.effort });
    await afterWrite(result);
    notice.value = switchedNotice(draft, 'facts');
  } catch (err) {
    notice.value = switchError(err);
    await load();
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    ref="panelRoot"
    class="access-panel engine-panel"
    role="dialog"
    aria-label="Движок"
  >
    <div class="access-panel-header">
      <strong>Движок</strong>
      <button
        type="button"
        class="access-panel-close"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </div>

    <p
      v-if="error !== null"
      class="engine-error"
    >
      {{ error }}
    </p>
    <p
      v-if="notice !== null"
      class="engine-notice"
      role="status"
    >
      {{ notice }}
    </p>

    <section
      v-if="agentDraft !== null"
      class="engine-block engine-agent"
      data-block="agent"
    >
      <h2>Мой агент</h2>
      <EngineChoice
        v-model="agentDraft"
        :status="status"
        :busy="busy"
        warn-no-images
      />
      <button
        type="button"
        class="engine-apply"
        :disabled="busy || !agentChanged"
        @click="applyAgent"
      >
        Применить
      </button>
      <EngineFallbackRow
        who="agent"
        :status="status"
        :main="agent?.engine ?? 'claude'"
        :busy="busy"
        @status="afterWrite"
        @notice="notice = $event"
        @busy="busy = $event"
      />
    </section>

    <section
      v-if="factsDraft !== null"
      class="engine-block engine-facts"
      data-block="facts"
    >
      <h2>Факты о людях</h2>
      <div class="engine-mode">
        <label>
          <input
            type="radio"
            name="engine-facts-mode"
            value="agent"
            :checked="factsFollow"
            :disabled="busy"
            @change="factsFollow = true"
          >
          как у агента
        </label>
        <label>
          <input
            type="radio"
            name="engine-facts-mode"
            value="own"
            :checked="!factsFollow"
            :disabled="busy"
            @change="chooseFactsOwn"
          >
          своё
        </label>
      </div>
      <EngineChoice
        v-if="!factsFollow"
        v-model="factsDraft"
        kind="facts"
        :status="status"
        :busy="busy"
      />
      <button
        type="button"
        class="engine-apply engine-apply-facts"
        :disabled="busy || !factsChanged"
        @click="applyFacts"
      >
        Применить
      </button>
      <EngineFallbackRow
        who="facts"
        :status="status"
        :main="facts?.engine ?? 'claude'"
        :busy="busy"
        @status="afterWrite"
        @notice="notice = $event"
        @busy="busy = $event"
      />
    </section>
  </div>
</template>
