<script setup lang="ts">
// Ф41-2б-2: форма «Новая сессия» — роль, номер (пусто = свободный), движок, модель (текст с подсказками по
// движку), «усилие» (по движку), название. Отказ ядра (занятая линия, потолок) — его русской строкой под формой.
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';

import * as api from '../api/client';

import type {
  AgentRequest,
  AgentRequestApproveBody,
  EngineName,
  ProjectConversation,
  SessionCreateInput,
} from '../api/types';
import { store } from '../store/instance';
import { loadRoles, rolesState } from '../store/roles';
import { autosendNote, buildWantSub, defaultSubKind, emptySubForm } from '../util/sessionSub';
import RoleTextModal from './RoleTextModal.vue';
import DictateButton from './DictateButton.vue';
import SessionSubFields from './SessionSubFields.vue';
import { sessionEngineChoices } from '../util/engineProfiles';
import {
  conversationLabel,
  effortChoices,
  effortForEngine,
  modelDefaultEffort,
  sessionModelHints,
} from '../util/sessions';

// Ф41-6a: без `projectId` (кнопка «Новая сессия» списка «Главной») в форме есть выбор проекта.
// Ф41-5: `request` — «Изменить и завести» по заявке агента: поля заявки подставлены, отправка = `approve` заявки,
// появляется флажок «может отвечать в беседы „без подтверждения“ своей роли» (`autosend_ok`).
const props = defineProps<{ projectId?: number; request?: AgentRequest }>();
const emit = defineEmits<{ done: []; cancel: [] }>();

const chosenProject = ref<number | null>(null);
const projectChoices = computed(() => store.state.projects.filter((p) => p.id > 0 && p.workdir));
const effectiveProject = computed<number | null>(
  () => props.projectId ?? chosenProject.value ?? projectChoices.value[0]?.id ?? null,
);
const role = ref(props.request?.payload?.role ?? '');
const number = ref('');
const asked = props.request?.payload?.engine;
// Ф74-1: движки — из профилей `/status` (старое ядро — запасные); заявка с движком не из списка — умолчание.
const engineChoices = computed(() => sessionEngineChoices());
const engine = ref<EngineName>(
  (engineChoices.value.find((p) => p.name === asked) ?? engineChoices.value[0]).name as EngineName,
);
const model = ref(props.request?.payload?.model ?? '');
const effort = ref(props.request?.payload?.effort ?? '');
// Ф41-5: режим сессии — «Ручной» (умолчание) или «Авто».
const mode = ref<'manual' | 'auto'>(props.request?.payload?.mode === 'auto' ? 'auto' : 'manual');
const autosendOk = ref(false);
// Ф41-8: роль из каталога хаба (список) или свой текст.
const rolePick = ref('');
const showRoleText = ref(false);
const pickedRole = computed(() => rolesState.items.find((r) => r.key === rolePick.value) ?? null);
// Ф48-3: файл роли — путь к MD-файлу в рабочей папке проекта (необязательно); у заявки агента поля нет.
const roleFile = ref(props.request?.payload?.role_file ?? '');
// Ф50-7: подпапка проекта (необязательно); после создания не меняется. У заявки — предзаполнена из заявки.
const subdir = ref(props.request?.payload?.subdir ?? '');
const roleFileWarning = ref<string | null>(null);
// Ф99-1: «Продолжить разговор» — прошлый разговор Claude Code папки проекта (по умолчанию — новый). Только движок
// Claude Code, без подпапки и не у заявки агента; список — из ядра по проекту, один раз на проект.
const resumeConv = ref('');
const conversations = ref<ProjectConversation[]>([]);
const conversationsFor = ref<number | null>(null);
const conversationsError = ref<string | null>(null);
const resumeWarning = ref<string | null>(null);
const resumeAvailable = computed(
  () => props.request === undefined && engine.value === 'claude' && subdir.value.trim() === '',
);
let conversationsSeq = 0;

async function loadConversations(projectId: number): Promise<void> {
  const mine = ++conversationsSeq;
  conversationsFor.value = projectId;
  conversationsError.value = null;
  try {
    const reply = await api.getProjectConversations(projectId);
    if (mine !== conversationsSeq) return;
    conversations.value = Array.isArray(reply?.items) ? reply.items : [];
  } catch {
    if (mine !== conversationsSeq) return;
    conversations.value = [];
    conversationsError.value = 'Список разговоров не загрузился.';
  }
}

watch(
  [resumeAvailable, effectiveProject],
  ([available, projectId]) => {
    if (!available) {
      resumeConv.value = '';
      return;
    }
    if (projectId === null || projectId === conversationsFor.value) return;
    resumeConv.value = '';
    conversations.value = [];
    void loadConversations(projectId);
  },
  { immediate: true },
);
const title = ref('');
// Первое задание (макет «Новая сессия»): уходит первой репликой сразу после создания; у заявки агента его нет.
const firstText = ref('');
const sub = reactive(emptySubForm());
let subTouched = false;
const autosendCount = ref(0);
let autosendTimer: ReturnType<typeof setTimeout> | null = null;
let autosendSeq = 0;
const busy = ref(false);
const error = ref<string | null>(null);

const hints = computed(() => sessionModelHints(engine.value));
const efforts = computed(() => effortChoices(engine.value, effort.value, model.value));
const defaultEffort = computed(() => modelDefaultEffort(engine.value, model.value));
// Смена движка: усилие, которого нет в его списке, сбрасывается в «по умолчанию».
watch(engine, (value) => {
  effort.value = effortForEngine(value, effort.value, model.value);
});
// Ф74-6: смена модели — усилие не из её списка сбрасывается в «по умолчанию».
watch(model, (value) => {
  effort.value = effortForEngine(engine.value, effort.value, value);
});
const listId = computed(() => `session-models-${effectiveProject.value ?? 0}`);
const numberValue = computed(() => {
  const text = number.value.trim();
  if (text === '') return null;
  return /^\d+$/.test(text) && Number(text) >= 1 ? Number(text) : NaN;
});
const numberBad = computed(() => numberValue.value !== null && Number.isNaN(numberValue.value));

// Умолчание блока «Подписка» следует за ролью, пока владелец сам его не менял.
watch(role, (value) => {
  if (!subTouched) sub.kind = defaultSubKind(value);
  if (autosendTimer !== null) clearTimeout(autosendTimer);
  const name = value.trim();
  if (name === '') {
    autosendCount.value = 0;
    return;
  }
  autosendTimer = setTimeout(async () => {
    const mine = ++autosendSeq;
    try {
      const { count } = await api.getSessionAutosendCount(effectiveProject.value ?? 0, name);
      if (mine === autosendSeq) autosendCount.value = typeof count === 'number' ? count : 0;
    } catch {
      if (mine === autosendSeq) autosendCount.value = 0; // строка только справочная — отказ ядра её просто не показывает
    }
  }, 250);
});
onBeforeUnmount(() => {
  if (autosendTimer !== null) clearTimeout(autosendTimer);
});
onMounted(() => {
  if (!rolesState.loaded) void loadRoles();
});

/** Ф41-8: выбор роли из списка подставляет первое имя и подписку по умолчанию (`direct` — только сообщения агентов,
 * `project` — карточки разборщика проекта); пустой выбор оставляет свой текст как есть. */
function onRolePick(): void {
  const picked = rolesState.items.find((r) => r.key === rolePick.value);
  if (picked === undefined) return;
  role.value = picked.names[0] ?? picked.title;
  subTouched = true;
  sub.kind = picked.sub === 'direct' ? 'direct' : 'cards';
}

let created = false;

async function submit(): Promise<void> {
  if (busy.value || numberBad.value) return;
  const built = buildWantSub(sub);
  if (!built.ok) {
    error.value = built.error;
    return;
  }
  if (effectiveProject.value === null) {
    error.value = 'Выбери проект.';
    return;
  }
  if (props.request !== undefined) {
    const body: AgentRequestApproveBody = { engine: engine.value, mode: mode.value, autosend_ok: autosendOk.value };
    if (role.value.trim() !== '') body.role = role.value.trim();
    if (model.value.trim() !== '') body.model = model.value.trim();
    if (effort.value !== '') body.effort = effort.value;
    if (built.value !== null) body.want_sub = built.value;
    // Ф50-7: оба ключа — всегда; пустая строка = «без папки / без файла» (к значению агента не откатывается).
    body.subdir = subdir.value.trim();
    body.role_file = roleFile.value.trim();
    busy.value = true;
    error.value = null;
    try {
      const result = await store.approveAgentRequest(props.request, body);
      if (result.ok) emit('done');
      else error.value = result.message;
    } finally {
      busy.value = false;
    }
    return;
  }
  const input: SessionCreateInput = { project_id: effectiveProject.value, engine: engine.value };
  if (mode.value === 'auto') input.mode = 'auto';
  if (role.value.trim() !== '') input.role = role.value.trim();
  if (numberValue.value !== null) input.number = numberValue.value;
  if (model.value.trim() !== '') input.model = model.value.trim();
  if (effort.value !== '') input.effort = effort.value;
  if (title.value.trim() !== '') input.title = title.value.trim();
  if (roleFile.value.trim() !== '') input.role_file = roleFile.value.trim();
  if (subdir.value.trim() !== '') input.subdir = subdir.value.trim();
  if (resumeAvailable.value && resumeConv.value !== '') input.resume_conv = resumeConv.value;
  if (built.value !== null) input.want_sub = built.value;
  busy.value = true;
  error.value = null;
  try {
    if (created) {
      // Сессия уже заведена, не ушло только задание: второе нажатие закрывает форму, а не заводит вторую сессию.
      emit('done');
      return;
    }
    const result = await store.createSession(input, firstText.value.trim());
    if (result.ok && (result.warning || result.resumeWarning)) {
      // Сессия заведена, но файл роли сейчас не прочитать или прошлый разговор менялся только что: форма остаётся с
      // предупреждением, второе нажатие закрывает.
      roleFileWarning.value = result.warning ?? null;
      resumeWarning.value = result.resumeWarning ?? null;
      created = true;
    } else if (result.ok) {
      emit('done');
    } else {
      error.value = result.message;
      if (result.message.startsWith('Сессия заведена')) created = true;
    }
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <form
    class="work-new-task session-new"
    aria-label="Новая сессия"
    @submit.prevent="submit"
  >
    <label
      v-if="props.projectId === undefined"
      class="work-field"
    >
      <span>Проект</span>
      <select
        :value="effectiveProject"
        class="session-new-project"
        @change="chosenProject = Number(($event.target as HTMLSelectElement).value)"
      >
        <option
          v-for="p in projectChoices"
          :key="p.id"
          :value="p.id"
        >
          {{ p.name }}
        </option>
      </select>
    </label>
    <label
      v-if="rolesState.items.length > 0"
      class="work-field"
    >
      <span>Роль из каталога</span>
      <select
        v-model="rolePick"
        class="session-new-role-pick"
        @change="onRolePick"
      >
        <option value="">
          своя (текстом ниже)
        </option>
        <option
          v-for="r in rolesState.items"
          :key="r.key"
          :value="r.key"
        >
          {{ r.title }}
        </option>
      </select>
    </label>
    <button
      v-if="pickedRole"
      type="button"
      class="link-button session-role-text-link"
      @click="showRoleText = true"
    >
      инструкция роли
    </button>
    <RoleTextModal
      v-if="showRoleText && pickedRole"
      :role-key="pickedRole.key"
      :title="pickedRole.title"
      @close="showRoleText = false"
    />
    <label class="work-field">
      <span>Роль (необязательно)</span>
      <input
        v-model="role"
        type="text"
        class="session-new-role"
        maxlength="100"
      >
    </label>
    <label class="work-field">
      <span>Файл роли (путь в проекте)</span>
      <input
        v-model="roleFile"
        type="text"
        class="session-new-role-file"
        maxlength="300"
        placeholder="docs/roles/ROLE.md"
        autocapitalize="off"
        spellcheck="false"
      >
      <small class="session-role-file-hint">MD-файл с инструкцией роли; подставляется в сессию дословно и не теряется при сжатии. Claude Code закрепляет инструкцию при создании сессии; правку файла сессия получит сообщением при следующем запуске, насовсем — в новой сессии. OpenCode читает файл при каждом запуске.</small>
    </label>
    <label class="work-field">
      <span>Папка в проекте (необязательно)</span>
      <input
        v-model="subdir"
        type="text"
        class="session-new-subdir"
        maxlength="300"
        placeholder="например, orders/zakaz-17"
        autocapitalize="off"
        spellcheck="false"
      >
      <small class="session-subdir-hint">относительно папки проекта; после создания не меняется</small>
    </label>
    <label class="work-field">
      <span>Номер (пусто — свободный)</span>
      <input
        v-model="number"
        type="text"
        inputmode="numeric"
        class="session-new-number"
        :aria-invalid="numberBad"
      >
    </label>
    <label class="work-field">
      <span>Движок</span>
      <select
        v-model="engine"
        class="session-new-engine"
      >
        <option
          v-for="p in engineChoices"
          :key="p.name"
          :value="p.name"
        >
          {{ p.label }}
        </option>
      </select>
    </label>
    <label
      v-if="resumeAvailable"
      class="work-field"
    >
      <span>Продолжить разговор</span>
      <select
        v-model="resumeConv"
        class="session-new-resume"
      >
        <option value="">
          новый разговор
        </option>
        <option
          v-for="c in conversations"
          :key="c.id"
          :value="c.id"
          :disabled="c.hub_session_id !== null"
        >
          {{ conversationLabel(c) }}
        </option>
      </select>
      <small
        v-if="conversationsError"
        class="work-error session-resume-error"
      >{{ conversationsError }}</small>
      <small
        v-else-if="resumeConv !== ''"
        class="session-resume-hint"
      >прошлый разговор Claude Code этой папки; если он открыт в другом окне — закрой его, иначе сессия не запустится</small>
    </label>
    <label class="work-field">
      <span>Модель</span>
      <input
        v-model="model"
        type="text"
        class="session-new-model"
        :list="listId"
        maxlength="200"
        autocomplete="off"
      >
      <datalist :id="listId">
        <option
          v-for="hint in hints"
          :key="hint"
          :value="hint"
        />
      </datalist>
    </label>
    <label class="work-field">
      <span>Усилие (необязательно)</span>
      <select
        v-model="effort"
        class="session-new-effort"
      >
        <option value="">
          по умолчанию{{ defaultEffort ? ` (${defaultEffort})` : '' }}
        </option>
        <option
          v-for="e in efforts"
          :key="e"
          :value="e"
        >
          {{ e }}
        </option>
      </select>
    </label>
    <label class="work-field">
      <span>Режим</span>
      <select
        v-model="mode"
        class="session-new-mode"
      >
        <option value="manual">
          Ручной
        </option>
        <option value="auto">
          Авто
        </option>
      </select>
    </label>
    <label
      v-if="request !== undefined"
      class="work-field work-field--check"
    >
      <input
        v-model="autosendOk"
        type="checkbox"
        class="session-new-autosend"
      >
      <span>может отвечать в беседы «без подтверждения» своей роли</span>
    </label>
    <label class="work-field">
      <span>Название (необязательно)</span>
      <input
        v-model="title"
        type="text"
        class="session-new-title"
        maxlength="200"
      >
    </label>
    <label
      v-if="request === undefined"
      class="work-field"
    >
      <span>Первое задание (необязательно)</span>
      <textarea
        v-model="firstText"
        class="session-new-first"
        rows="3"
        maxlength="20000"
        placeholder="Что сделать сразу после запуска"
      />
      <DictateButton />
    </label>
    <SessionSubFields
      :form="sub"
      @kind="subTouched = true"
    />
    <p
      v-if="role.trim() !== '' && autosendCount > 0"
      class="session-sub-autosend"
    >
      {{ autosendNote(autosendCount) }}
    </p>
    <div class="work-actions">
      <button
        type="submit"
        class="session-new-submit"
        :disabled="busy || numberBad"
      >
        {{ roleFileWarning !== null || resumeWarning !== null ? 'Закрыть' : request !== undefined ? 'Завести' : 'Создать' }}
      </button>
      <button
        type="button"
        class="link-button"
        @click="emit('cancel')"
      >
        отмена
      </button>
    </div>
    <p
      v-if="roleFileWarning !== null"
      class="session-model-warning session-role-file-warning"
      role="status"
    >
      Сессия заведена. Но {{ roleFileWarning }} — она запустится без файла роли, пока его не будет.
    </p>
    <p
      v-if="resumeWarning !== null"
      class="session-model-warning session-resume-warning"
      role="status"
    >
      Сессия заведена. Но {{ resumeWarning }}.
    </p>
    <p
      v-if="numberBad"
      class="work-error"
    >
      Номер — целое число от 1.
    </p>
    <p
      v-if="error"
      class="work-error"
      role="alert"
    >
      {{ error }}
    </p>
  </form>
</template>
