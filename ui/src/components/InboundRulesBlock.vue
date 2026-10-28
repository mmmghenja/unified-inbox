<script setup lang="ts">
// Ф63-1 (`docs/architecture.md`, 9): блок «Правила входящих» панели «Раскладка».
// Правила проекта: условие словами (`description` от ядра) · действие · «сработало N» · выключатель · «Удалить» с
// подтверждением · форма добавления. На телефоне — только список и выключатель. Все строки от сервера (названия,
// описания, ключи, роли) — только текстом.
import { computed, ref, watch } from 'vue';

import * as api from '../api/client';
import type { InboundRule, InboundRuleInput } from '../api/types';
import { store } from '../store/instance';
import { isPhone } from '../util/layout';

const KIND_LABELS: Array<[string, string]> = [
  ['dm', 'личка'],
  ['group', 'группа'],
  ['channel', 'канал'],
  ['topic', 'тема форума'],
  ['mail_thread', 'письмо'],
];

const projectId = ref('');
const rules = ref<InboundRule[]>([]);
const limits = ref<{ rules: number } | null>(null);
const loadError = ref<string | null>(null);
const error = ref<string | null>(null);
const busy = ref(false);
const pendingDelete = ref<number | null>(null);

const realProjects = computed(() => store.state.projects.filter((p) => p.id > 0));
const accounts = computed(() => store.state.status?.accounts ?? []);
const phone = computed(() => isPhone());

function errorText(err: unknown): string {
  if (err instanceof api.ApiError) return err.message || `Ошибка ${err.status}.`;
  if (err instanceof api.NetworkError) return 'Нет связи с хабом — попробуй ещё раз.';
  return 'Не удалось — попробуй ещё раз.';
}

async function load(): Promise<void> {
  rules.value = [];
  loadError.value = null;
  pendingDelete.value = null;
  const id = Number(projectId.value);
  if (!Number.isInteger(id) || id <= 0) return;
  try {
    const data = await api.listInboundRules(id);
    if (projectId.value !== String(id)) return;
    rules.value = data.rules;
    limits.value = data.limits;
  } catch (err) {
    loadError.value = errorText(err);
  }
}

watch(projectId, () => void load());

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

function actionText(rule: InboundRule): string {
  const parts: string[] = [];
  if (rule.action.wake.length > 0) parts.push(`будит: ${rule.action.wake.join(', ')}`);
  if (rule.action.attention === 'react') parts.push('внимание: реагирую');
  else if (rule.action.attention === 'info') parts.push('внимание: к сведению');
  if (rule.action.archive === true) parts.push('сразу в архив');
  return parts.join(' · ');
}

function hitsText(rule: InboundRule): string {
  if (rule.hits === 0) return 'не срабатывало';
  const last = rule.last_hit_ts === null ? '' : `, последний раз ${new Date(rule.last_hit_ts).toLocaleString('ru-RU')}`;
  return `сработало ${rule.hits}${last}`;
}

function silentRoles(rule: InboundRule): string[] {
  return rule.wake_status.filter((s) => s.listening === 'none').map((s) => s.role);
}

async function toggle(rule: InboundRule): Promise<void> {
  await act(async () => {
    const updated = await api.patchInboundRule(rule.project_id, rule.id, { enabled: !rule.enabled });
    rules.value = rules.value.map((r) => (r.id === updated.id ? updated : r));
  });
}

async function remove(rule: InboundRule): Promise<void> {
  await act(async () => {
    await api.deleteInboundRule(rule.project_id, rule.id);
    pendingDelete.value = null;
    rules.value = rules.value.filter((r) => r.id !== rule.id);
  });
}

// --- Форма ---------------------------------------------------------------------------------------------------

const f = ref({
  title: '',
  accounts: [] as number[],
  from: '',
  subject: '',
  text: '',
  exclude: '',
  kinds: [] as string[],
  wake: '',
  attention: '' as '' | 'react' | 'info',
  archive: false,
});

/** Список из строки: запятые или переводы строк (фразу ключа можно писать с пробелами). */
function split(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter((s) => s !== '');
}

async function add(): Promise<void> {
  const id = Number(projectId.value);
  const body: InboundRuleInput = {
    title: f.value.title,
    accounts: f.value.accounts,
    from: split(f.value.from),
    subject: split(f.value.subject),
    text: split(f.value.text),
    exclude: split(f.value.exclude),
    kinds: f.value.kinds,
    wake: split(f.value.wake),
    attention: f.value.attention === '' ? null : f.value.attention,
    archive: f.value.archive ? true : null,
  };
  await act(async () => {
    const created = await api.addInboundRule(id, body);
    rules.value = [...rules.value, created];
    f.value = { title: '', accounts: [], from: '', subject: '', text: '', exclude: '', kinds: [], wake: '', attention: '', archive: false };
  });
}
</script>

<template>
  <section class="access-section inbound-rules">
    <h2>Правила входящих</h2>
    <p class="mapping-hint">
      Правило проекта: письма и сообщения по условию будят сессию с ролью и/или меняют внимание («реагирую» /
      «к сведению») или сразу убирают беседу в архив. Ручной режим беседы и заглушка сильнее правила.
    </p>
    <label>
      Проект
      <select
        v-model="projectId"
        class="inbound-project"
      >
        <option value="">—</option>
        <option
          v-for="p in realProjects"
          :key="p.id"
          :value="String(p.id)"
        >
          {{ p.name }}
        </option>
      </select>
    </label>
    <p
      v-if="loadError !== null"
      class="access-error"
    >
      {{ loadError }}
    </p>
    <p
      v-if="error !== null"
      class="access-error inbound-error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-if="projectId !== '' && loadError === null && rules.length === 0"
      class="mapping-empty"
    >
      Правил входящих пока нет.
    </p>
    <ul class="mapping-rule-list inbound-rule-list">
      <li
        v-for="rule in rules"
        :key="rule.id"
        class="mapping-rule inbound-rule"
        :data-inbound-rule-id="rule.id"
      >
        <span class="mapping-rule-what">
          <strong>#{{ rule.id }} {{ rule.title }}</strong>
          — {{ rule.description }} → {{ actionText(rule) }}
        </span>
        <span class="mapping-rule-count">
          {{ hitsText(rule) }}
          <template v-if="rule.created_by_role !== null"> · завела роль «{{ rule.created_by_role }}»</template>
          <template v-if="silentRoles(rule).length > 0"> · не слушает: {{ silentRoles(rule).join(', ') }}</template>
        </span>
        <label class="inbound-toggle">
          <input
            type="checkbox"
            class="inbound-enabled"
            :checked="rule.enabled"
            :disabled="busy"
            @change="toggle(rule)"
          >
          включено
        </label>
        <template v-if="!phone">
          <button
            type="button"
            class="mapping-rule-delete inbound-delete"
            :disabled="busy"
            @click="pendingDelete = rule.id"
          >
            Удалить
          </button>
          <div
            v-if="pendingDelete === rule.id"
            class="mapping-confirm"
          >
            <p>Удалить правило #{{ rule.id }}? Отметки «правило #{{ rule.id }}» у сообщений останутся без названия.</p>
            <div class="access-row">
              <button
                type="button"
                class="inbound-delete-confirm"
                :disabled="busy"
                @click="remove(rule)"
              >
                Да, удалить
              </button>
              <button
                type="button"
                class="inbound-delete-cancel"
                @click="pendingDelete = null"
              >
                Отмена
              </button>
            </div>
          </div>
        </template>
      </li>
    </ul>

    <form
      v-if="!phone && projectId !== ''"
      class="mapping-add inbound-add"
      @submit.prevent="add"
    >
      <h3>Добавить правило</h3>
      <label>
        Название (до 80 знаков)
        <input
          v-model="f.title"
          class="inbound-title"
          type="text"
          maxlength="80"
        >
      </label>
      <fieldset>
        <legend>Аккаунты (пусто — все)</legend>
        <label
          v-for="a in accounts"
          :key="a.id"
        >
          <input
            v-model="f.accounts"
            class="inbound-account"
            type="checkbox"
            :value="a.id"
          >
          {{ a.title ?? a.source }}
        </label>
      </fieldset>
      <label>
        Отправители (домен или адрес, через запятую; только почта)
        <input
          v-model="f.from"
          class="inbound-from"
          type="text"
        >
      </label>
      <label>
        Ключи темы (через запятую, фраза до 6 слов)
        <input
          v-model="f.subject"
          class="inbound-subject"
          type="text"
        >
      </label>
      <label>
        Ключи текста
        <input
          v-model="f.text"
          class="inbound-text"
          type="text"
        >
      </label>
      <label>
        Исключения
        <input
          v-model="f.exclude"
          class="inbound-exclude"
          type="text"
        >
      </label>
      <fieldset>
        <legend>Виды бесед (пусто — все, кроме трекеров)</legend>
        <label
          v-for="[kind, label] in KIND_LABELS"
          :key="kind"
        >
          <input
            v-model="f.kinds"
            class="inbound-kind"
            type="checkbox"
            :value="kind"
          >
          {{ label }}
        </label>
      </fieldset>
      <label>
        Будить роли (через запятую)
        <input
          v-model="f.wake"
          class="inbound-wake"
          type="text"
        >
      </label>
      <label>
        Внимание
        <select
          v-model="f.attention"
          class="inbound-attention"
        >
          <option value="">не менять</option>
          <option value="react">реагирую</option>
          <option value="info">к сведению</option>
        </select>
      </label>
      <label class="inbound-archive-label">
        <input
          v-model="f.archive"
          class="inbound-archive"
          type="checkbox"
        >
        Сразу в архив (беседа уходит в архив, карточек и сигналов нет)
      </label>
      <p
        v-if="limits !== null"
        class="mapping-hint"
      >
        Правил в проекте: {{ rules.length }} из {{ limits.rules }}.
      </p>
      <button
        type="submit"
        class="inbound-submit"
        :disabled="busy || f.title.trim() === ''"
      >
        Добавить
      </button>
    </form>
  </section>
</template>
