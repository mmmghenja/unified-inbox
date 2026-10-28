<script setup lang="ts">
// Ф14-1 C3 (`docs/architecture.md`): панель «Раскладка» — из правого блока `TopBar` и из верхней
// полосы `PhoneShell` (как «Доступ»). Две части:
// - «Правила» по аккаунтам: «<описание> → <проект>» · «сейчас N бесед» · «Удалить» (через предпросмотр
//   `DELETE ?dry_run=true`); «Добавить правило»: аккаунт -> `map-options` -> условие -> значение -> проект ->
//   «Проверить» (`dry_run`) -> «Добавить». «Добавить» активна только после «Проверить» с теми же полями.
// - «Проекты»: цвет, название («Сохранить», когда изменено), есть ли рабочая папка; «Новый проект».
// Все строки от сервера (описания правил, названия бесед, папок, меток, доменов, проектов) — только текстом.
import { computed, onMounted, reactive, ref, watch } from 'vue';

import * as api from '../api/client';
import InboundRulesBlock from './InboundRulesBlock.vue';
import ProjectOrderBlock from './ProjectOrderBlock.vue';
import type { MapOptions, MapPreview, MapRule, MapRuleInput, Project } from '../api/types';
import { store } from '../store/instance';
import {
  addPreviewLine,
  besed,
  colorInputValue,
  deletePreviewLine,
  exampleLine,
  freeColor,
  optionCountLabel,
  projectName,
  RULE_KIND_LABELS,
  type RuleKind,
} from '../util/mapping';
import { accountAsleep, CHANNEL_SLEEP_NOTE } from '../util/workday';
import { usePanel } from '../util/popup';
import ProjectFallbackRow from './ProjectFallbackRow.vue';

const emit = defineEmits<{ close: [] }>();
// Ф80-1 B1: Esc и нажатие мимо закрывают панель (общий механизм `util/popup`).
const panelRoot = ref<HTMLElement | null>(null);
usePanel(() => panelRoot.value, () => emit('close'));

const busy = ref(false);
const notice = ref<string | null>(null);
const error = ref<string | null>(null);

const projects = computed(() => store.state.projects);
const realProjects = computed(() => store.state.projects.filter((p) => p.id > 0));
const accounts = computed(() => store.state.status?.accounts ?? []);

function accountTitle(accountId: number): string {
  const account = accounts.value.find((a) => a.id === accountId);
  return account === undefined ? `аккаунт #${accountId}` : (account.title ?? account.source);
}

function errorText(err: unknown): string {
  if (err instanceof api.ApiError) return err.message || `Ошибка ${err.status}.`;
  if (err instanceof api.NetworkError) return 'Нет связи с хабом — попробуй ещё раз.';
  return 'Не удалось — попробуй ещё раз.';
}

async function act(fn: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  notice.value = null;
  error.value = null;
  try {
    await fn();
  } catch (err) {
    error.value = errorText(err);
  } finally {
    busy.value = false;
  }
}

// --- Правила ------------------------------------------------------------------------------------------------

const rules = ref<MapRule[]>([]);
const rulesError = ref<string | null>(null);

async function loadRules(): Promise<void> {
  try {
    rules.value = await api.listMapRules();
    rulesError.value = null;
  } catch {
    rulesError.value = 'Не удалось загрузить правила.';
  }
}

/** Правила по аккаунтам; внутри — порядок ядра (порядок проверки). */
const rulesByAccount = computed(() => {
  const groups: Array<{ accountId: number; title: string; rules: MapRule[] }> = [];
  for (const rule of rules.value) {
    let group = groups.find((g) => g.accountId === rule.account_id);
    if (group === undefined) {
      group = { accountId: rule.account_id, title: accountTitle(rule.account_id), rules: [] };
      groups.push(group);
    }
    group.rules.push(rule);
  }
  return groups;
});

const pendingDelete = ref<{ ruleId: number; preview: MapPreview } | null>(null);

async function askDelete(rule: MapRule): Promise<void> {
  await act(async () => {
    pendingDelete.value = { ruleId: rule.id, preview: await api.deleteMapRule(rule.id, { dryRun: true }) };
  });
}

async function confirmDelete(): Promise<void> {
  const pending = pendingDelete.value;
  if (pending === null) return;
  await act(async () => {
    const result = await api.deleteMapRule(pending.ruleId);
    pendingDelete.value = null;
    notice.value = `Правило удалено, переложено: ${besed(result.channels_relaid)}.`;
    await loadRules();
    store.refreshAfterMapping();
  });
}

// --- Добавить правило ---------------------------------------------------------------------------------------

const formAccount = ref('');
const formKind = ref<RuleKind | ''>('');
const formValue = ref('');
const formProject = ref('');
const options = ref<MapOptions | null>(null);
const optionsError = ref<string | null>(null);
const preview = ref<{ key: string; data: MapPreview } | null>(null);

const fieldsKey = computed(() => JSON.stringify([formAccount.value, formKind.value, formValue.value, formProject.value]));

// Любая правка поля сбрасывает предпросмотр: «Добавить» — только после «Проверить» с текущими полями.
watch(fieldsKey, () => {
  preview.value = null;
});

const previewValid = computed(() => preview.value !== null && preview.value.key === fieldsKey.value);

async function onAccountChange(): Promise<void> {
  options.value = null;
  optionsError.value = null;
  formKind.value = '';
  formValue.value = '';
  const accountId = Number(formAccount.value);
  if (!Number.isInteger(accountId) || accountId <= 0) return;
  try {
    const loaded = await api.getMapOptions(accountId);
    if (formAccount.value === String(accountId)) options.value = loaded;
  } catch (err) {
    optionsError.value = errorText(err);
  }
}

function onKindChange(): void {
  formValue.value = '';
}

function buildInput(): MapRuleInput | null {
  const accountId = Number(formAccount.value);
  const projectId = Number(formProject.value);
  const kind = formKind.value;
  if (!Number.isInteger(accountId) || accountId <= 0 || !Number.isInteger(projectId) || projectId <= 0) return null;
  if (kind === '') return null;
  if (kind === 'all') return { account_id: accountId, project_id: projectId, kind, value: null };
  const raw = formValue.value.trim();
  if (raw === '') return null;
  if (kind === 'folder') {
    const folderId = Number(raw);
    return Number.isInteger(folderId) ? { account_id: accountId, project_id: projectId, kind, value: folderId } : null;
  }
  return { account_id: accountId, project_id: projectId, kind, value: raw };
}

const canCheck = computed(() => !busy.value && buildInput() !== null);
const canAdd = computed(() => !busy.value && previewValid.value && buildInput() !== null);

async function check(): Promise<void> {
  const input = buildInput();
  if (input === null) return;
  const key = fieldsKey.value;
  await act(async () => {
    const data = await api.addMapRule(input, { dryRun: true });
    if (key === fieldsKey.value) preview.value = { key, data };
  });
}

async function addRule(): Promise<void> {
  const input = buildInput();
  if (input === null || !previewValid.value) return;
  await act(async () => {
    const result = await api.addMapRule(input);
    notice.value = `Правило #${result.rule_id} добавлено, переложено: ${besed(result.channels_relaid)}.`;
    formKind.value = '';
    formValue.value = '';
    preview.value = null;
    await loadRules();
    store.refreshAfterMapping();
  });
}

// --- Проекты ------------------------------------------------------------------------------------------------

const nameEdits = reactive<Record<number, string>>({});

function nameOf(project: Project): string {
  return nameEdits[project.id] ?? project.name ?? '';
}

function onNameInput(project: Project, event: Event): void {
  nameEdits[project.id] = (event.target as HTMLInputElement).value;
}

function nameChanged(project: Project): boolean {
  const edited = nameEdits[project.id];
  return edited !== undefined && edited.trim() !== (project.name ?? '');
}

async function saveName(project: Project): Promise<void> {
  const name = (nameEdits[project.id] ?? '').trim();
  await act(async () => {
    await api.patchProject(project.id, { name });
    delete nameEdits[project.id];
    notice.value = `Проект «${name}» переименован.`;
    await store.loadProjects();
  });
}

// Ф37-3: отметка «разобрано» сессиями у проекта — ключ `session_handled`.
async function onHandledChange(project: Project, event: Event): Promise<void> {
  const on = (event.target as HTMLInputElement).checked;
  await act(async () => {
    await api.patchProject(project.id, { session_handled: on });
    await store.loadProjects();
  });
}

// Ф64-1: «Проект по времени» — рабочий день кнопками («Закончить день» / «Начать день»); ключ `workday`.
async function onWorkdayChange(project: Project, event: Event): Promise<void> {
  const on = (event.target as HTMLInputElement).checked;
  await act(async () => {
    await api.patchProject(project.id, { workday: on });
    await store.loadProjects();
  });
}

// Ф86-1: сводка дня после сжатия (`digest_after_compact`) и слова владельца из соседних сессий (`sibling_says`);
// нет поля у проекта (старое ядро) — галочка стоит: по умолчанию оба включены.
async function onDigestFlagChange(
  project: Project,
  key: 'digest_after_compact' | 'sibling_says',
  event: Event,
): Promise<void> {
  const on = (event.target as HTMLInputElement).checked;
  await act(async () => {
    await api.patchProject(project.id, { [key]: on });
    await store.loadProjects();
  });
}

// Ф48-4: сообщения агентов подпискам проекта пачкой — ключ `agent_batch_min` (минуты, 0 — сразу).
async function onAgentBatchChange(project: Project, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const minutes = Number(input.value);
  if (!Number.isInteger(minutes) || minutes < 0 || minutes > 60) {
    input.value = String(project.agent_batch_min ?? 0);
    error.value = 'Сообщения агентов пачкой: целое число минут от 0 до 60 (0 — сразу).';
    return;
  }
  if (minutes === (project.agent_batch_min ?? 0)) return;
  await act(async () => {
    await api.patchProject(project.id, { agent_batch_min: minutes });
    await store.loadProjects();
  });
}

// Ф52-2: аккаунты, чьё «Неразобранное» слышат сессии проекта — ключ `unsorted_accounts` (список целиком).
async function onUnsortedChange(project: Project, accountId: number, event: Event): Promise<void> {
  const on = (event.target as HTMLInputElement).checked;
  const current = project.unsorted_accounts ?? [];
  const next = on ? [...new Set([...current, accountId])].sort((a, b) => a - b) : current.filter((id) => id !== accountId);
  await act(async () => {
    await api.patchProject(project.id, { unsorted_accounts: next });
    await store.loadProjects();
  });
}

// Ф17-1b: режим вступления сессий в группы Telegram — ключ `session_join` проекта.
async function onJoinModeChange(project: Project, event: Event): Promise<void> {
  const mode = (event.target as HTMLSelectElement).value === 'auto' ? 'auto' : 'confirm';
  await act(async () => {
    await api.patchProject(project.id, { session_join: mode });
    await store.loadProjects();
  });
}

async function onColorChange(project: Project, event: Event): Promise<void> {
  const color = (event.target as HTMLInputElement).value;
  await act(async () => {
    await api.patchProject(project.id, { color });
    await store.loadProjects();
  });
}

const newName = ref('');
const newColor = ref(freeColor(store.state.projects));

async function createProject(): Promise<void> {
  const name = newName.value.trim();
  if (name === '') return;
  await act(async () => {
    const { project_id } = await api.createProject({ name, color: newColor.value });
    notice.value = `Проект «${name}» создан (#${project_id}).`;
    newName.value = '';
    await store.loadProjects();
    newColor.value = freeColor(store.state.projects);
  });
}

onMounted(() => {
  void loadRules();
});
</script>

<template>
  <div
    ref="panelRoot"
    class="access-panel mapping-panel"
    role="dialog"
    aria-label="Раскладка"
  >
    <div class="access-panel-header">
      <strong>Раскладка</strong>
      <button
        type="button"
        class="access-panel-close"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </div>

    <p
      v-if="notice !== null"
      class="access-notice"
      role="status"
    >
      {{ notice }}
    </p>
    <p
      v-if="error !== null"
      class="access-error mapping-error"
      role="alert"
    >
      {{ error }}
    </p>

    <section class="access-section mapping-rules">
      <h2>Правила</h2>
      <p class="mapping-hint">
        Правила проверяются сверху вниз, первое подошедшее решает. Беседа, перенесённая вручную, правилами больше не
        двигается — пока в её шапке не выбрать «По правилам».
      </p>
      <p
        v-if="rulesError !== null"
        class="access-error"
      >
        {{ rulesError }}
      </p>
      <div
        v-for="group in rulesByAccount"
        :key="group.accountId"
        class="mapping-account"
        :data-account-id="group.accountId"
      >
        <h3 class="mapping-account-title">
          {{ group.title }}
          <span
            v-if="accountAsleep(accounts, group.accountId)"
            class="channel-sleep-mark"
            data-test="account-sleep"
          >{{ CHANNEL_SLEEP_NOTE }}</span>
        </h3>
        <ul class="mapping-rule-list">
          <li
            v-for="rule in group.rules"
            :key="rule.id"
            class="mapping-rule"
            :data-rule-id="rule.id"
          >
            <span class="mapping-rule-what">{{ rule.description }} → {{ projectName(projects, rule.project_id) }}</span>
            <span class="mapping-rule-count">сейчас {{ besed(rule.channels) }}</span>
            <button
              type="button"
              class="mapping-rule-delete"
              :disabled="busy"
              @click="askDelete(rule)"
            >
              Удалить
            </button>
            <div
              v-if="pendingDelete !== null && pendingDelete.ruleId === rule.id"
              class="mapping-confirm"
            >
              <p>{{ deletePreviewLine(pendingDelete.preview.moved) }}</p>
              <ul class="mapping-examples">
                <li
                  v-for="example in pendingDelete.preview.examples"
                  :key="example.channel_id"
                >
                  {{ exampleLine(projects, example) }}
                </li>
              </ul>
              <div class="access-row">
                <button
                  type="button"
                  class="mapping-delete-confirm"
                  :disabled="busy"
                  @click="confirmDelete"
                >
                  Да, удалить
                </button>
                <button
                  type="button"
                  class="mapping-delete-cancel"
                  @click="pendingDelete = null"
                >
                  Отмена
                </button>
              </div>
            </div>
          </li>
        </ul>
      </div>
      <p
        v-if="rules.length === 0 && rulesError === null"
        class="mapping-empty"
      >
        Правил пока нет.
      </p>

      <form
        class="mapping-add"
        @submit.prevent="check"
      >
        <h3>Добавить правило</h3>
        <label>
          Аккаунт
          <select
            v-model="formAccount"
            class="mapping-add-account"
            @change="onAccountChange"
          >
            <option value="">—</option>
            <option
              v-for="account in accounts"
              :key="account.id"
              :value="String(account.id)"
            >
              {{ account.title ?? account.source }}
            </option>
          </select>
        </label>
        <p
          v-if="optionsError !== null"
          class="access-error"
        >
          {{ optionsError }}
        </p>
        <label v-if="options !== null">
          Условие
          <select
            v-model="formKind"
            class="mapping-add-kind"
            @change="onKindChange"
          >
            <option value="">—</option>
            <option
              v-for="kind in options.kinds"
              :key="kind"
              :value="kind"
            >
              {{ RULE_KIND_LABELS[kind] }}
            </option>
          </select>
        </label>
        <label v-if="options !== null && formKind === 'folder'">
          Папка
          <select
            v-model="formValue"
            class="mapping-add-value"
          >
            <option value="">—</option>
            <option
              v-for="folder in options.folders"
              :key="folder.id"
              :value="String(folder.id)"
            >
              {{ optionCountLabel(folder.title, folder.channels, folder.unsorted) }}
            </option>
          </select>
        </label>
        <label v-if="options !== null && formKind === 'label'">
          Метка
          <select
            v-model="formValue"
            class="mapping-add-value"
          >
            <option value="">—</option>
            <option
              v-for="label in options.labels"
              :key="label.name"
              :value="label.name"
            >
              {{ optionCountLabel(label.name, label.channels, label.unsorted) }}
            </option>
          </select>
        </label>
        <label v-if="options !== null && formKind === 'domain'">
          Домен отправителя
          <input
            v-model="formValue"
            type="text"
            class="mapping-add-value"
            list="mapping-domains"
            autocomplete="off"
            autocapitalize="off"
            spellcheck="false"
            placeholder="example.com"
          >
          <datalist id="mapping-domains">
            <option
              v-for="domain in options.domains"
              :key="domain.domain"
              :value="domain.domain"
              :label="optionCountLabel(domain.domain, domain.channels, domain.unsorted)"
            />
          </datalist>
        </label>
        <label v-if="options !== null && formKind !== ''">
          Проект
          <select
            v-model="formProject"
            class="mapping-add-project"
          >
            <option value="">—</option>
            <option
              v-for="project in realProjects"
              :key="project.id"
              :value="String(project.id)"
            >
              {{ project.name ?? `проект #${project.id}` }}
            </option>
          </select>
        </label>
        <div
          v-if="previewValid && preview !== null"
          class="mapping-preview"
        >
          <p>{{ addPreviewLine(preview.data.moved) }}</p>
          <ul class="mapping-examples">
            <li
              v-for="example in preview.data.examples"
              :key="example.channel_id"
            >
              {{ exampleLine(projects, example) }}
            </li>
          </ul>
        </div>
        <div class="access-row">
          <button
            type="submit"
            class="mapping-check"
            :disabled="!canCheck"
          >
            Проверить
          </button>
          <button
            type="button"
            class="mapping-add-submit"
            :disabled="!canAdd"
            @click="addRule"
          >
            Добавить
          </button>
        </div>
      </form>
    </section>

    <ProjectOrderBlock />

    <section class="access-section mapping-projects">
      <h2>Проекты</h2>
      <ul class="mapping-project-list">
        <li
          v-for="project in realProjects"
          :key="project.id"
          class="mapping-project"
          :data-project-id="project.id"
        >
          <div class="access-row">
            <input
              type="color"
              class="mapping-project-color"
              :aria-label="`Цвет проекта ${project.name ?? project.id}`"
              :value="colorInputValue(project.color)"
              :disabled="busy"
              @change="onColorChange(project, $event)"
            >
            <input
              type="text"
              class="mapping-project-name"
              aria-label="Название проекта"
              maxlength="40"
              :value="nameOf(project)"
              @input="onNameInput(project, $event)"
            >
            <button
              v-if="nameChanged(project)"
              type="button"
              class="mapping-project-save"
              :disabled="busy"
              @click="saveName(project)"
            >
              Сохранить
            </button>
          </div>
          <p class="mapping-project-workdir">
            сессии Claude: {{ project.workdir ? 'есть рабочая папка' : 'нет' }}
          </p>
          <label class="mapping-project-join">
            Вступление в группы Telegram:
            <select
              class="mapping-project-join-mode"
              :aria-label="`Вступление в группы Telegram — проект ${project.name ?? project.id}`"
              :value="project.session_join ?? 'confirm'"
              :disabled="busy"
              @change="onJoinModeChange(project, $event)"
            >
              <option value="confirm">с подтверждением</option>
              <option value="auto">сразу (до 5 в день)</option>
            </select>
          </label>
          <p class="mapping-hint">
            сразу — только для подписанной сессии проекта
          </p>
          <label class="mapping-project-handled">
            <input
              type="checkbox"
              class="mapping-project-handled-box"
              :aria-label="`Сессии могут отмечать беседы разобранными — проект ${project.name ?? project.id}`"
              :checked="project.session_handled === true"
              :disabled="busy"
              @change="onHandledChange(project, $event)"
            >
            Сессии могут отмечать беседы разобранными
          </label>
          <label class="mapping-project-workday">
            <input
              type="checkbox"
              class="mapping-project-workday-box"
              :aria-label="`Проект по времени — ${project.name ?? project.id}`"
              :checked="project.workday === true"
              :disabled="busy"
              @change="onWorkdayChange(project, $event)"
            >
            Проект по времени (рабочий день кнопками)
          </label>
          <label class="mapping-project-digest">
            <input
              type="checkbox"
              class="mapping-project-digest-box"
              :aria-label="`Сводка дня сессиям после сжатия — проект ${project.name ?? project.id}`"
              :checked="project.digest_after_compact !== false"
              :disabled="busy"
              @change="onDigestFlagChange(project, 'digest_after_compact', $event)"
            >
            Сводка дня сессиям после сжатия
          </label>
          <label class="mapping-project-siblings">
            <input
              type="checkbox"
              class="mapping-project-siblings-box"
              :aria-label="`Мои слова из соседних сессий — проект ${project.name ?? project.id}`"
              :checked="project.sibling_says !== false"
              :disabled="busy"
              @change="onDigestFlagChange(project, 'sibling_says', $event)"
            >
            Сессиям — мои слова из соседних сессий проекта
          </label>
          <label class="mapping-project-agent-batch">
            Сообщения агентов пачкой, мин:
            <input
              type="number"
              class="mapping-project-agent-batch-min"
              min="0"
              max="60"
              step="1"
              inputmode="numeric"
              :aria-label="`Сообщения агентов пачкой, минут — проект ${project.name ?? project.id}`"
              :title="'0 — сразу; сообщения владельца и срочные приходят сразу всегда'"
              :value="project.agent_batch_min ?? 0"
              :disabled="busy"
              @change="onAgentBatchChange(project, $event)"
            >
          </label>
          <ProjectFallbackRow
            :project="project"
            :busy="busy"
            @saved="store.loadProjects()"
            @error="error = $event"
          />
          <div
            v-if="accounts.length > 0"
            class="mapping-project-unsorted"
          >
            Слышит неразобранное аккаунтов
            <p class="mapping-hint">
              сессии проекта получают сигнал о новых сообщениях в беседах этих аккаунтов без проекта
            </p>
            <label
              v-for="account in accounts"
              :key="account.id"
              class="mapping-project-unsorted-item"
            >
              <input
                type="checkbox"
                class="mapping-project-unsorted-box"
                :aria-label="`Слышит неразобранное аккаунта ${account.source} ${account.title ?? account.id} — проект ${project.name ?? project.id}`"
                :checked="(project.unsorted_accounts ?? []).includes(account.id)"
                :disabled="busy"
                @change="onUnsortedChange(project, account.id, $event)"
              >
              {{ account.source }} · {{ account.title ?? 'без названия' }} · аккаунт {{ account.id }}
            </label>
          </div>
          <p
            v-if="!project.workdir"
            class="mapping-hint"
          >
            Рабочую папку для сессий Claude задаёт команда в терминале:
            ~/projects/hub-prod/.venv/bin/hub project set {{ project.id }} --workdir и полный путь к папке.
          </p>
        </li>
      </ul>
      <form
        class="mapping-new-project"
        @submit.prevent="createProject"
      >
        <h3>Новый проект</h3>
        <div class="access-row">
          <input
            v-model="newColor"
            type="color"
            class="mapping-new-color"
            aria-label="Цвет нового проекта"
          >
          <input
            v-model="newName"
            type="text"
            class="mapping-new-name"
            aria-label="Название нового проекта"
            maxlength="40"
            placeholder="Название"
          >
          <button
            type="submit"
            class="mapping-new-create"
            :disabled="busy || newName.trim() === ''"
          >
            Создать
          </button>
        </div>
      </form>
    </section>

    <InboundRulesBlock />
  </div>
</template>
