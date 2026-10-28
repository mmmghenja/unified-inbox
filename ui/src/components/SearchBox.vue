<script setup lang="ts">
// Поиск по переписке (прежде — в `TopBar.vue`; Ф10-6 — общий для верхней полосы и «Бесед» телефона). Клик по
// результату открывает его проект и беседу. Название беседы и текст — недоверенный ввод: только интерполяция.
import { onBeforeUnmount, ref, watch } from 'vue';

import * as api from '../api/client';
import type { SearchResult } from '../api/types';
import { store } from '../store/instance';

// Ф20-4 A2: в меню «⋯» поиск открывается с фокусом в поле (`focus()`), а выбор результата сообщает `picked` —
// меню закрывается.
const emit = defineEmits<{ picked: [] }>();

const input = ref<HTMLInputElement | null>(null);
const query = ref('');
const results = ref<SearchResult[]>([]);
const searching = ref(false);
// Ф80-1 F1: поиск идёт по вводу с задержкой, а не только по Enter; «ничего не найдено» и ошибка сети — текстом.
const DEBOUNCE_MS = 300;
const searched = ref(false);
const failure = ref<string | null>(null);
let timer: ReturnType<typeof setTimeout> | null = null;
let seq = 0;

function clearTimer(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

async function runSearch(): Promise<void> {
  clearTimer();
  const q = query.value.trim();
  const mine = ++seq;
  failure.value = null;
  if (q.length < 2) {
    results.value = [];
    searched.value = false;
    return;
  }
  searching.value = true;
  try {
    const found = await api.search(q);
    if (mine !== seq) return; // пришёл ответ на устаревший запрос
    results.value = found;
    searched.value = true;
  } catch {
    if (mine !== seq) return;
    results.value = [];
    searched.value = false;
    failure.value = 'Не удалось выполнить поиск — проверь связь и попробуй ещё раз.';
  } finally {
    if (mine === seq) searching.value = false;
  }
}

watch(query, () => {
  clearTimer();
  timer = setTimeout(() => void runSearch(), DEBOUNCE_MS);
});
onBeforeUnmount(clearTimer);

function openResult(result: SearchResult): void {
  clearTimer();
  seq++;
  results.value = [];
  searched.value = false;
  query.value = '';
  clearTimer();
  void store.openSearchResult(result);
  emit('picked');
}

defineExpose({ focus: () => input.value?.focus() });
</script>

<template>
  <form
    class="search-form"
    role="search"
    @submit.prevent="runSearch"
  >
    <input
      ref="input"
      v-model="query"
      type="search"
      placeholder="Поиск…"
      aria-label="Поиск по переписке"
    >
    <p
      v-if="searching"
      class="search-status"
      role="status"
    >
      Ищу…
    </p>
    <p
      v-else-if="failure !== null"
      class="search-status search-status--error"
      role="alert"
    >
      {{ failure }}
    </p>
    <p
      v-else-if="searched && results.length === 0"
      class="search-status"
      role="status"
    >
      Ничего не найдено.
    </p>
    <ul
      v-if="results.length > 0"
      class="search-results"
    >
      <li
        v-for="result in results"
        :key="result.message_id"
      >
        <button
          type="button"
          @click="openResult(result)"
        >
          <strong class="search-result-title">{{ result.channel_title ?? '(без названия)' }}</strong>
          <span class="search-result-text">{{ result.text ?? '' }}</span>
        </button>
      </li>
    </ul>
  </form>
</template>
