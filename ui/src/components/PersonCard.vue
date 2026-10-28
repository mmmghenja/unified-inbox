<script setup lang="ts">
// Ф21-1b (`docs/architecture.md`): карточка человека — окно поверх страницы (на телефоне —
// на весь экран). Открывается кликом по имени отправителя (`of-contact`) или строкой панели «Люди» (`person`).
// Имя (карандаш — переименовать, пусто — вернуть автоматическое), источники с «Отделить» (при 2+), «Объединить
// с…» (поиск -> выбор -> подтверждение), проекты, беседы (клик — открыть беседу и закрыть окно), цифры словами.
// 404 `not_person`/`is_me` по контакту — имя этого контакта больше не кликается (`store/people.ts`) и окно
// закрывается. Всё от сервера — только текстом.
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import * as api from '../api/client';
import type { PersonBrief, PersonCard } from '../api/types';
import { store } from '../store/instance';
import { closePerson, markNotPerson, type PersonTarget } from '../store/people';
import {
  NO_NAME,
  FACTS_CAPTION,
  FACTS_EMPTY,
  FACT_REMOVE_CONFIRM,
  NO_STATS_TEXT,
  computedLine,
  factDate,
  mergeConfirm,
  personErrorText,
  projectTitles,
  sourceBadge,
  sourceHandle,
  sourceLabel,
  statsLines,
} from '../util/people';

const props = defineProps<{ target: PersonTarget }>();

const dialog = ref<HTMLElement | null>(null);
const card = ref<PersonCard | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);
const busy = ref(false);

let ticket = 0;

function errorText(err: unknown): string {
  if (err instanceof api.ApiError) return personErrorText(err.status, err.message);
  if (err instanceof api.NetworkError) return 'Нет связи с хабом — попробуй ещё раз.';
  return 'Не удалось — попробуй ещё раз.';
}

async function load(): Promise<void> {
  const mine = ++ticket;
  loading.value = true;
  error.value = null;
  const target = props.target;
  try {
    const result = 'contactId' in target ? await api.getPersonOfContact(target.contactId) : await api.getPerson(target.personId);
    if (mine !== ticket) return;
    card.value = result;
  } catch (err) {
    if (mine !== ticket) return;
    if (
      'contactId' in target &&
      err instanceof api.ApiError &&
      err.status === 404 &&
      (err.message === 'not_person' || err.message === 'is_me')
    ) {
      markNotPerson(target.contactId);
      closePerson();
      return;
    }
    card.value = null;
    error.value = errorText(err);
  } finally {
    if (mine === ticket) loading.value = false;
  }
}

watch(() => props.target, () => void load());

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  event.stopPropagation();
  closePerson();
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown);
  void load();
  void nextTick(() => dialog.value?.focus());
});
onBeforeUnmount(() => {
  ticket += 1;
  document.removeEventListener('keydown', onKeydown);
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

// --- Имя ----------------------------------------------------------------------------------------------------

const renaming = ref(false);
const nameDraft = ref('');

function startRename(): void {
  if (card.value === null) return;
  nameDraft.value = card.value.name_manual ? card.value.name : '';
  renaming.value = true;
}

async function saveRename(value: string | null): Promise<void> {
  const current = card.value;
  if (current === null) return;
  await act(async () => {
    const trimmed = value === null ? null : value.trim();
    card.value = await api.renamePerson(current.id, trimmed === '' ? null : trimmed);
    renaming.value = false;
  });
}

// --- Отделить -----------------------------------------------------------------------------------------------

async function split(contactId: number): Promise<void> {
  const current = card.value;
  if (current === null) return;
  await act(async () => {
    await api.splitPerson([contactId]);
    card.value = await api.getPerson(current.id);
  });
}

// --- Объединить с… ------------------------------------------------------------------------------------------

const merging = ref(false);
const mergeQuery = ref('');
const mergeHits = ref<PersonBrief[]>([]);
const mergePick = ref<PersonBrief | null>(null);
let searchTicket = 0;

async function search(): Promise<void> {
  const mine = ++searchTicket;
  const q = mergeQuery.value.trim();
  if (q === '') {
    mergeHits.value = [];
    return;
  }
  try {
    const result = await api.listPeople({ q, scope: 'all', limit: 10 });
    if (mine !== searchTicket) return;
    mergeHits.value = result.items.filter((p) => p.id !== card.value?.id);
  } catch (err) {
    if (mine !== searchTicket) return;
    error.value = errorText(err);
  }
}

function startMerge(): void {
  merging.value = true;
  mergeQuery.value = '';
  mergeHits.value = [];
  mergePick.value = null;
}

function cancelMerge(): void {
  merging.value = false;
  mergePick.value = null;
}

async function confirmMerge(): Promise<void> {
  const current = card.value;
  const pick = mergePick.value;
  if (current === null || pick === null) return;
  await act(async () => {
    card.value = await api.mergePeople(current.id, [pick.id]);
    cancelMerge();
  });
}

// --- Что известно (Ф24-1c) ----------------------------------------------------------------------------------

const editingFact = ref<number | 'new' | null>(null);
const factDraft = ref('');

function startEditFact(id: number, text: string): void {
  editingFact.value = id;
  factDraft.value = text;
}

function startAddFact(): void {
  editingFact.value = 'new';
  factDraft.value = '';
}

async function saveFact(): Promise<void> {
  const current = card.value;
  const which = editingFact.value;
  const text = factDraft.value.trim();
  if (current === null || which === null || text === '') return;
  await act(async () => {
    card.value =
      which === 'new'
        ? await api.addPersonFact(current.id, text)
        : await api.editPersonFact(current.id, which, text);
    editingFact.value = null;
  });
}

async function removeFact(factId: number): Promise<void> {
  const current = card.value;
  if (current === null || !window.confirm(FACT_REMOVE_CONFIRM)) return;
  await act(async () => {
    card.value = await api.removePersonFact(current.id, factId);
  });
}

// --- Беседа -------------------------------------------------------------------------------------------------

function openChannel(projectId: number | null, channelId: number): void {
  closePerson();
  void store.openChannelAt(projectId, channelId);
}
</script>

<template>
  <div
    class="person-overlay"
    @click.self="closePerson()"
  >
    <div
      ref="dialog"
      class="person-modal"
      role="dialog"
      aria-modal="true"
      aria-label="Карточка человека"
      tabindex="-1"
    >
      <button
        type="button"
        class="person-close"
        aria-label="Закрыть"
        @click="closePerson()"
      >
        ✕
      </button>

      <p
        v-if="error !== null"
        class="access-error person-error"
        role="alert"
      >
        {{ error }}
      </p>
      <p
        v-if="loading && card === null"
        class="person-loading"
      >
        Загружаю…
      </p>

      <template v-if="card !== null">
        <header class="person-head">
          <template v-if="!renaming">
            <h2 class="person-name">
              {{ card.name || NO_NAME }}
            </h2>
            <button
              type="button"
              class="person-pencil"
              aria-label="Переименовать"
              title="Переименовать"
              @click="startRename"
            >
              ✎
            </button>
          </template>
          <form
            v-else
            class="person-rename"
            @submit.prevent="saveRename(nameDraft)"
          >
            <input
              v-model="nameDraft"
              type="text"
              maxlength="200"
              aria-label="Имя человека"
              placeholder="Пусто — автоматическое имя"
            >
            <button
              type="submit"
              :disabled="busy"
            >
              Сохранить
            </button>
            <button
              type="button"
              @click="renaming = false"
            >
              Отмена
            </button>
          </form>
        </header>
        <p
          v-if="card.names.length > 1"
          class="person-names"
        >
          Ещё: {{ card.names.filter((n) => n !== card?.name).join(', ') }}
        </p>
        <p
          v-if="card.name_manual && !renaming"
          class="person-names"
        >
          Имя задано тобой.
          <button
            type="button"
            class="person-reset"
            :disabled="busy"
            @click="saveRename(null)"
          >
            Вернуть автоматическое
          </button>
        </p>

        <section class="person-section person-sources">
          <h3>Источники</h3>
          <ul>
            <li
              v-for="contact in card.contacts"
              :key="contact.contact_id"
              class="person-source"
              :data-contact-id="contact.contact_id"
            >
              <span
                class="person-badge"
                :title="sourceLabel(contact.source)"
              >{{ sourceBadge(contact.source) }}</span>
              <span class="person-source-what">
                {{ contact.name ?? NO_NAME }}
                <span class="person-muted">{{ sourceHandle(contact) }}</span>
                <span
                  v-if="contact.account_title"
                  class="person-muted"
                > · {{ contact.account_title }}</span>
              </span>
              <button
                v-if="card.contacts.length >= 2"
                type="button"
                class="person-split"
                :disabled="busy"
                @click="split(contact.contact_id)"
              >
                Отделить
              </button>
            </li>
          </ul>

          <button
            v-if="!merging"
            type="button"
            class="person-merge-open"
            @click="startMerge"
          >
            Объединить с…
          </button>
          <div
            v-else
            class="person-merge"
          >
            <template v-if="mergePick === null">
              <input
                v-model="mergeQuery"
                type="search"
                maxlength="100"
                placeholder="Найти человека: имя, ник, почта"
                aria-label="Найти человека для объединения"
                @input="search"
              >
              <ul class="person-hits">
                <li
                  v-for="hit in mergeHits"
                  :key="hit.id"
                >
                  <button
                    type="button"
                    class="person-hit"
                    @click="mergePick = hit"
                  >
                    {{ hit.name || NO_NAME }}
                    <span class="person-muted">{{ hit.sources.map((s) => sourceLabel(s.source)).join(', ') }}</span>
                  </button>
                </li>
              </ul>
              <button
                type="button"
                @click="cancelMerge"
              >
                Отмена
              </button>
            </template>
            <template v-else>
              <p class="person-merge-confirm">
                {{ mergeConfirm(card.name || NO_NAME, mergePick.name || NO_NAME) }}
              </p>
              <button
                type="button"
                class="person-merge-do"
                :disabled="busy"
                @click="confirmMerge"
              >
                Объединить
              </button>
              <button
                type="button"
                @click="mergePick = null"
              >
                Назад
              </button>
            </template>
          </div>
        </section>

        <section
          v-if="projectTitles(card.projects).length > 0"
          class="person-section person-projects"
        >
          <h3>Проекты</h3>
          <p>{{ projectTitles(card.projects).join(', ') }}</p>
        </section>

        <section
          v-if="card.channels.length > 0"
          class="person-section person-channels"
        >
          <h3>Беседы</h3>
          <ul>
            <li
              v-for="channel in card.channels"
              :key="channel.channel_id"
            >
              <button
                type="button"
                class="person-channel"
                @click="openChannel(channel.project_id, channel.channel_id)"
              >
                {{ channel.title ?? `беседа #${channel.channel_id}` }}
              </button>
              <span class="person-muted"> · {{ channel.in }} от него, {{ channel.out }} от тебя</span>
            </li>
          </ul>
        </section>

        <section class="person-section person-stats">
          <h3>Цифры</h3>
          <template v-if="card.stats !== null">
            <p
              v-for="line in statsLines(card.stats)"
              :key="line"
              class="person-stat"
            >
              {{ line }}
            </p>
          </template>
          <p
            v-else
            class="person-muted person-no-stats"
          >
            {{ NO_STATS_TEXT }}
          </p>
          <p
            v-if="card.stats !== null"
            class="person-muted person-computed"
          >
            {{ computedLine(card.stats_ts) }}
          </p>
        </section>
        <section class="person-section person-facts">
          <h3>Что известно</h3>
          <p
            v-if="card.facts.items.length === 0 && editingFact !== 'new'"
            class="person-muted person-facts-empty"
          >
            {{ FACTS_EMPTY }}
          </p>
          <ul>
            <li
              v-for="fact in card.facts.items"
              :key="fact.id"
              class="person-fact"
              :data-fact-id="fact.id"
            >
              <form
                v-if="editingFact === fact.id"
                class="person-fact-form"
                @submit.prevent="saveFact"
              >
                <input
                  v-model="factDraft"
                  type="text"
                  maxlength="300"
                  aria-label="Текст факта"
                >
                <button
                  type="submit"
                  :disabled="busy"
                >
                  Сохранить
                </button>
                <button
                  type="button"
                  @click="editingFact = null"
                >
                  Отмена
                </button>
              </form>
              <template v-else>
                <span
                  class="person-fact-text"
                  :title="factDate(fact.updated_ts)"
                >{{ fact.text }}</span>
                <span
                  v-if="fact.source === 'manual'"
                  class="person-badge person-fact-mine"
                  title="Записано вами"
                >вы</span>
                <button
                  type="button"
                  class="person-fact-edit"
                  aria-label="Править факт"
                  title="Править"
                  :disabled="busy"
                  @click="startEditFact(fact.id, fact.text)"
                >
                  ✎
                </button>
                <button
                  type="button"
                  class="person-fact-remove"
                  aria-label="Убрать факт"
                  title="Убрать"
                  :disabled="busy"
                  @click="removeFact(fact.id)"
                >
                  ×
                </button>
              </template>
            </li>
          </ul>
          <form
            v-if="editingFact === 'new'"
            class="person-fact-form person-fact-new"
            @submit.prevent="saveFact"
          >
            <input
              v-model="factDraft"
              type="text"
              maxlength="300"
              aria-label="Новый факт"
            >
            <button
              type="submit"
              :disabled="busy"
            >
              Сохранить
            </button>
            <button
              type="button"
              @click="editingFact = null"
            >
              Отмена
            </button>
          </form>
          <button
            v-else
            type="button"
            class="person-fact-add"
            :disabled="busy"
            @click="startAddFact"
          >
            + Добавить факт
          </button>
          <p class="person-muted person-facts-caption">
            {{ FACTS_CAPTION }}
          </p>
        </section>
      </template>
    </div>
  </div>
</template>
