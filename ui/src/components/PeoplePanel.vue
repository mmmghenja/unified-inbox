<script setup lang="ts">
// Ф21-1b (`docs/architecture.md`): панель «Люди» — пункт меню «⋯». Поиск, «Знакомые / Все»,
// строки (имя, значки источников, последний контакт, «отвечаешь ~12 мин»), подгрузка порциями по 50; сверху блок
// «Похоже, один человек» (`GET /people/suggestions`): «A (Telegram) и B (Slack) · Объединить · Нет». Клик по
// строке — `PersonCard`. Всё от сервера — только текстом.
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';

import * as api from '../api/client';
import type { PersonBrief, PersonSuggestion } from '../api/types';
import { openPerson, peopleUi } from '../store/people';
import {
  NO_NAME,
  lastContactText,
  personErrorText,
  replyMeBrief,
  sourceBadge,
  sourceLabel,
  suggestionLine,
} from '../util/people';
import { usePanel } from '../util/popup';

const emit = defineEmits<{ close: [] }>();
// Ф80-1 B1: Esc и нажатие мимо закрывают панель (общий механизм `util/popup`).
const panelRoot = ref<HTMLElement | null>(null);
usePanel(() => panelRoot.value, () => emit('close'));

const PAGE = 50;
const SEARCH_DELAY_MS = 250;

const query = ref('');
const scope = ref<'known' | 'all'>('known');
const items = ref<PersonBrief[]>([]);
const total = ref(0);
const loading = ref(false);
const error = ref<string | null>(null);
const suggestions = ref<PersonSuggestion[]>([]);
const busy = ref(false);

let ticket = 0;
let timer: ReturnType<typeof setTimeout> | null = null;

function errorText(err: unknown): string {
  if (err instanceof api.ApiError) return personErrorText(err.status, err.message);
  if (err instanceof api.NetworkError) return 'Нет связи с хабом — попробуй ещё раз.';
  return 'Не удалось — попробуй ещё раз.';
}

/** `append` — подгрузка следующей порции; иначе список начинается заново. */
async function load(append: boolean): Promise<void> {
  const mine = ++ticket;
  loading.value = true;
  error.value = null;
  try {
    const result = await api.listPeople({
      q: query.value.trim(),
      scope: scope.value,
      limit: PAGE,
      offset: append ? items.value.length : 0,
    });
    if (mine !== ticket) return;
    items.value = append ? [...items.value, ...result.items] : result.items;
    total.value = result.total;
  } catch (err) {
    if (mine !== ticket) return;
    error.value = errorText(err);
  } finally {
    if (mine === ticket) loading.value = false;
  }
}

async function loadSuggestions(): Promise<void> {
  try {
    suggestions.value = await api.getPeopleSuggestions();
  } catch {
    suggestions.value = [];
  }
}

function scheduleSearch(): void {
  if (timer !== null) clearTimeout(timer);
  timer = setTimeout(() => void load(false), SEARCH_DELAY_MS);
}

watch(scope, () => void load(false));

// Карточка закрыта — в ней могли объединить, отделить или переименовать: список и предложения обновляем.
watch(
  () => peopleUi.card,
  (now, before) => {
    if (now === null && before !== null) {
      void load(false);
      void loadSuggestions();
    }
  },
);

onMounted(() => {
  void load(false);
  void loadSuggestions();
});
onBeforeUnmount(() => {
  ticket += 1;
  if (timer !== null) clearTimeout(timer);
});

async function act(fn: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = null;
  try {
    await fn();
  } catch (err) {
    error.value = errorText(err);
  } finally {
    busy.value = false;
  }
}

async function mergeSuggestion(pair: PersonSuggestion): Promise<void> {
  await act(async () => {
    await api.mergePeople(pair.a.id, [pair.b.id]);
    await Promise.all([loadSuggestions(), load(false)]);
  });
}

async function rejectSuggestion(pair: PersonSuggestion): Promise<void> {
  await act(async () => {
    await api.notSamePeople(pair.a.id, pair.b.id);
    suggestions.value = suggestions.value.filter((s) => s !== pair);
  });
}
</script>

<template>
  <div
    ref="panelRoot"
    class="access-panel people-panel"
    role="dialog"
    aria-label="Люди"
  >
    <div class="access-panel-header">
      <strong>Люди</strong>
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
      class="access-error people-error"
      role="alert"
    >
      {{ error }}
    </p>

    <section
      v-if="suggestions.length > 0"
      class="people-suggestions"
    >
      <h2>Похоже, один человек</h2>
      <ul>
        <li
          v-for="pair in suggestions"
          :key="`${pair.a.id}-${pair.b.id}`"
          class="people-suggestion"
        >
          <span class="people-suggestion-what">{{ suggestionLine(pair.a, pair.b) }}</span>
          <button
            type="button"
            class="people-suggestion-merge"
            :disabled="busy"
            @click="mergeSuggestion(pair)"
          >
            Объединить
          </button>
          <button
            type="button"
            class="people-suggestion-no"
            :disabled="busy"
            @click="rejectSuggestion(pair)"
          >
            Нет
          </button>
        </li>
      </ul>
    </section>

    <div class="people-controls">
      <input
        v-model="query"
        type="search"
        maxlength="100"
        placeholder="Имя, ник или почта"
        aria-label="Поиск людей"
        @input="scheduleSearch"
      >
      <div
        class="people-scope"
        role="group"
        aria-label="Кого показывать"
      >
        <button
          type="button"
          class="people-scope-known"
          :aria-pressed="scope === 'known'"
          @click="scope = 'known'"
        >
          Знакомые
        </button>
        <button
          type="button"
          class="people-scope-all"
          :aria-pressed="scope === 'all'"
          @click="scope = 'all'"
        >
          Все
        </button>
      </div>
    </div>

    <ul class="people-list">
      <li
        v-for="person in items"
        :key="person.id"
      >
        <button
          type="button"
          class="people-row"
          :data-person-id="person.id"
          @click="openPerson(person.id)"
        >
          <span class="people-row-name">{{ person.name || NO_NAME }}</span>
          <span class="people-row-badges">
            <span
              v-for="source in [...new Set(person.sources.map((s) => s.source))]"
              :key="source"
              class="person-badge"
              :title="sourceLabel(source)"
            >{{ sourceBadge(source) }}</span>
          </span>
          <span class="people-row-meta person-muted">
            {{ [lastContactText(person), replyMeBrief(person)].filter((t) => t !== null).join(' · ') }}
          </span>
        </button>
      </li>
    </ul>
    <p
      v-if="!loading && items.length === 0 && error === null"
      class="person-muted people-empty"
    >
      Никого не нашлось.
    </p>
    <button
      v-if="items.length < total"
      type="button"
      class="people-more"
      :disabled="loading"
      @click="load(true)"
    >
      Показать ещё ({{ total - items.length }})
    </button>
  </div>
</template>
