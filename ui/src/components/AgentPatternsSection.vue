<script setup lang="ts">
// Ф76-4б (docs/architecture.md): раздел «Мой агент» в «⋯ → Автопилот» — модель (общая и своя
// у проекта), правила владельца, выученные образцы, последние решения агента с «неверно». «Все проекты» = id 0.
// Тексты правил, образцов и решений — данные: только интерполяцией. Работает и с телефона (кука интерфейса).
import { computed, ref, watch } from 'vue';

import * as api from '../api/client';
import type { AgentDecision, PatternsState } from '../api/types';
import { store } from '../store/instance';
import { basisLabel, clockLabel, exampleText, patternStatusLabel, toggleKey } from '../util/agentPatterns';
import AgentModelPicker from './AgentModelPicker.vue';

const RULES_MAX = 8000;

const scope = ref(0);
const state = ref<PatternsState | null>(null);
const decisions = ref<AgentDecision[]>([]);
const rulesText = ref('');
const loadError = ref<string | null>(null);
const decisionsMissing = ref(false);
const error = ref<string | null>(null);
const saved = ref<string | null>(null);
const busy = ref(false);
const openExamples = ref<Set<string>>(new Set());
const dropped = ref<Set<number>>(new Set());

const projects = computed(() => store.state.projects.filter((p) => p.id > 0));

async function load(): Promise<void> {
  loadError.value = null;
  decisionsMissing.value = false;
  error.value = null;
  saved.value = null;
  dropped.value = new Set();
  const id = scope.value;
  try {
    const s = await api.getPatterns(id);
    if (id !== scope.value) return;
    if (!Array.isArray(s?.patterns)) throw new Error('bad');
    state.value = s;
    rulesText.value = s.rules ?? '';
  } catch {
    if (id === scope.value) loadError.value = 'образцы не загрузились';
  }
  try {
    const d = await api.getAgentDecisions(id, 30);
    if (id === scope.value) decisions.value = Array.isArray(d?.items) ? d.items : [];
  } catch {
    // Ручки журнала может ещё не быть (404) — раздел просто пуст.
    if (id === scope.value) {
      decisions.value = [];
      decisionsMissing.value = true;
    }
  }
}

watch(scope, load, { immediate: true });

function fail(): void {
  error.value = 'не сохранилось: нет связи с хабом или ядро отказало';
}

async function saveRules(): Promise<void> {
  busy.value = true;
  error.value = null;
  saved.value = null;
  try {
    const text = rulesText.value.trim();
    await api.putPatterns(scope.value, { rules: text === '' ? null : text });
    saved.value = 'правила сохранены';
  } catch {
    fail();
  } finally {
    busy.value = false;
  }
}

async function mark(field: 'keep' | 'drop' | 'always_me', key: string): Promise<void> {
  const s = state.value;
  if (s === null) return;
  busy.value = true;
  error.value = null;
  try {
    await api.putPatterns(scope.value, { [field]: toggleKey(s[field], key) });
    await load();
  } catch {
    fail();
  } finally {
    busy.value = false;
  }
}

async function wrong(d: AgentDecision): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const res = await api.markDecisionWrong(d.id);
    decisions.value = decisions.value.map((x) => (x.id === d.id ? { ...x, wrong_ts: res.wrong_ts ?? Date.now() } : x));
    if (res.dropped) dropped.value = new Set([...dropped.value, d.id]);
  } catch {
    fail();
  } finally {
    busy.value = false;
  }
}

function toggleExamples(key: string): void {
  const next = new Set(openExamples.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  openExamples.value = next;
}

function on(field: 'keep' | 'drop' | 'always_me', key: string): boolean {
  return state.value?.[field].includes(key) ?? false;
}
</script>

<template>
  <section
    class="agent-patterns"
    data-test="agent-patterns"
  >
    <strong>Мой агент</strong>
    <label class="work-field">
      <span>Для</span>
      <select
        v-model.number="scope"
        data-test="ap-scope"
      >
        <option :value="0">Все проекты</option>
        <option
          v-for="p in projects"
          :key="p.id"
          :value="p.id"
        >{{ p.name }}</option>
      </select>
    </label>

    <div class="ap-block">
      <AgentModelPicker
        :key="scope"
        :project-id="scope === 0 ? undefined : scope"
      />
    </div>

    <p
      v-if="loadError !== null"
      class="work-error"
      role="alert"
      data-test="ap-load-error"
    >{{ loadError }}</p>

    <div class="ap-block">
      <label class="work-field">
        <span>Правила владельца</span>
        <textarea
          v-model="rulesText"
          class="ap-rules"
          rows="4"
          :maxlength="RULES_MAX"
          placeholder="Например: пуш в main — только по моему слову"
          data-test="ap-rules"
        />
      </label>
      <button
        type="button"
        :disabled="busy"
        data-test="ap-rules-save"
        @click="saveRules"
      >
        Сохранить
      </button>
      <span
        v-if="saved !== null"
        class="agent-model-saved"
        data-test="ap-saved"
      >{{ saved }}</span>
    </div>

    <div
      v-if="state !== null"
      class="ap-block"
    >
      <strong>Выученные образцы</strong>
      <p
        class="agent-model-hint"
        data-test="ap-threshold"
      >
        образец — от {{ state.min_count }} одинаковых решений за {{ state.window_days }} дней
      </p>
      <p
        v-if="state.patterns.length === 0"
        class="board-empty"
      >
        Образцов пока нет.
      </p>
      <ul
        v-else
        class="ap-list"
      >
        <li
          v-for="pt in state.patterns"
          :key="pt.key"
          class="ap-item"
          :class="{ 'ap-item--off': pt.status === 'dropped' || on('drop', pt.key) }"
          data-test="ap-pattern"
        >
          <span class="ap-line">{{ pt.line }}</span>
          <span class="ap-meta">{{ pt.count }} раз · {{ pt.last_ts ? clockLabel(pt.last_ts) : '—' }} · {{ patternStatusLabel(pt.status) }}<template v-if="on('always_me', pt.key)"> · всегда спрашивать меня</template></span>
          <div class="ap-actions">
            <button
              type="button"
              class="route-btn"
              :class="{ 'route-btn--on': on('keep', pt.key) }"
              :aria-pressed="on('keep', pt.key)"
              :disabled="busy"
              data-test="ap-keep"
              @click="mark('keep', pt.key)"
            >
              оставить
            </button>
            <button
              type="button"
              class="route-btn"
              :class="{ 'route-btn--on': on('drop', pt.key) }"
              :aria-pressed="on('drop', pt.key)"
              :disabled="busy"
              data-test="ap-drop"
              @click="mark('drop', pt.key)"
            >
              убрать
            </button>
            <button
              type="button"
              class="route-btn"
              :class="{ 'route-btn--on': on('always_me', pt.key) }"
              :aria-pressed="on('always_me', pt.key)"
              :disabled="busy"
              data-test="ap-always"
              @click="mark('always_me', pt.key)"
            >
              всегда спрашивать меня
            </button>
            <button
              v-if="(pt.examples ?? []).length > 0"
              type="button"
              class="link-button"
              :aria-expanded="openExamples.has(pt.key)"
              data-test="ap-examples-toggle"
              @click="toggleExamples(pt.key)"
            >
              примеры {{ openExamples.has(pt.key) ? '▾' : '▸' }}
            </button>
          </div>
          <ul
            v-if="openExamples.has(pt.key)"
            class="ap-examples"
          >
            <li
              v-for="(e, i) in pt.examples"
              :key="i"
              data-test="ap-example"
            >{{ exampleText(e) }}</li>
          </ul>
        </li>
      </ul>
    </div>

    <div class="ap-block">
      <strong>Последние решения агента</strong>
      <p
        v-if="decisions.length === 0"
        class="board-empty"
        data-test="ap-decisions-empty"
      >
        {{ decisionsMissing ? 'Журнал решений пока недоступен.' : 'Агент ещё ничего не решал.' }}
      </p>
      <ul
        v-else
        class="ap-list"
      >
        <li
          v-for="d in decisions"
          :key="d.id"
          class="ap-item"
          :class="{ 'ap-item--off': d.wrong_ts !== null }"
          data-test="ap-decision"
        >
          <span class="ap-meta">{{ clockLabel(d.ts) }} · {{ d.role ?? 'без роли' }} · {{ d.kind }}</span>
          <span class="ap-line">{{ d.action ?? d.verdict ?? '—' }}<template v-if="d.text"> — {{ d.text }}</template></span>
          <span
            class="ap-meta"
            data-test="ap-basis"
          >{{ basisLabel(d.basis) }}</span>
          <span
            v-if="d.wrong_ts !== null"
            class="ap-meta"
            data-test="ap-wrong-mark"
          >помечено неверным<template v-if="dropped.has(d.id)"> · образец убран</template></span>
          <button
            v-else
            type="button"
            class="link-button"
            :disabled="busy"
            data-test="ap-wrong"
            @click="wrong(d)"
          >
            неверно
          </button>
        </li>
      </ul>
    </div>

    <p
      v-if="error !== null"
      class="work-error"
      role="alert"
      data-test="ap-error"
    >{{ error }}</p>
  </section>
</template>

<style scoped>
.agent-patterns {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 1rem;
  min-width: 0;
}
.ap-block {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  min-width: 0;
}
.ap-rules {
  width: 100%;
  box-sizing: border-box;
}
.ap-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.ap-item {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-width: 0;
  overflow-wrap: anywhere;
}
.ap-item--off {
  opacity: 0.55;
}
.ap-meta {
  font-size: 0.8em;
  opacity: 0.75;
}
.ap-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}
.ap-examples {
  margin: 0;
  padding-left: 1rem;
  font-size: 0.85em;
}
</style>
