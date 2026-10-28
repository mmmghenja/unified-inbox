<script setup lang="ts">
// «Роль…» из «⋯» окна сессии: сменить имя роли (пусто — снять), номер линии, галку «без подтверждения».
// Применится со следующего входа: хост перезапускает сессию, когда она закончит ход.
// Ф48-3: файл роли (путь к MD-файлу в проекте; пусто — снять). Ф48-5: Claude Code закрепляет инструкцию при
// создании сессии — правку файла сессия получает сообщением при следующем запуске.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import * as api from '../api/client';
import type { HubSession } from '../api/types';
import { store } from '../store/instance';
import { loadRoles, rolesState } from '../store/roles';
import { autosendNote } from '../util/sessionSub';
import { catalogRoleFor } from '../util/sessions';
import RoleTextModal from './RoleTextModal.vue';

const props = defineProps<{ session: HubSession }>();
const emit = defineEmits<{ done: []; cancel: [] }>();

const current = props.session.role?.trim() ?? '';
const role = ref(current);
const number = ref('');
const autosendOk = ref(props.session.autosend_ok ?? false);
const autosendTouched = ref(false);
const currentFile = props.session.role_file ?? '';
const roleFile = ref(currentFile);
const fileChanged = computed(() => roleFile.value.trim() !== currentFile);
const saved = ref(false);
const warning = ref<string | null>(null);
const rolePick = ref('');
const autosendCount = ref(0);
const showText = ref(false);
const busy = ref(false);
const error = ref<string | null>(null);
let timer: ReturnType<typeof setTimeout> | null = null;
let seq = 0;

const numberValue = computed(() => {
  const text = number.value.trim();
  if (text === '') return null;
  return /^\d+$/.test(text) && Number(text) >= 1 ? Number(text) : NaN;
});
const numberBad = computed(() => numberValue.value !== null && Number.isNaN(numberValue.value));
const roleChanged = computed(() => role.value.trim() !== current);
const picked = computed(() => rolesState.items.find((r) => r.key === rolePick.value) ?? null);
const catalogRole = computed(() => picked.value ?? catalogRoleFor(role.value, rolesState.items));

watch(role, (value, old) => {
  if (value.trim().toLowerCase() !== old.trim().toLowerCase()) {
    // Имя роли изменилось — галка сбрасывается; поставить её можно заново.
    autosendOk.value = value.trim().toLowerCase() === current.toLowerCase() ? (props.session.autosend_ok ?? false) : false;
    autosendTouched.value = false;
  }
  if (timer !== null) clearTimeout(timer);
  const name = value.trim();
  if (name === '') {
    autosendCount.value = 0;
    return;
  }
  timer = setTimeout(async () => {
    const mine = ++seq;
    try {
      const { count } = await api.getSessionAutosendCount(props.session.project_id, name);
      if (mine === seq) autosendCount.value = typeof count === 'number' ? count : 0;
    } catch {
      if (mine === seq) autosendCount.value = 0;
    }
  }, 250);
});
onMounted(() => {
  if (!rolesState.loaded) void loadRoles();
});
onBeforeUnmount(() => {
  if (timer !== null) clearTimeout(timer);
});

function onRolePick(): void {
  const r = picked.value;
  if (r === null) return;
  role.value = r.names[0] ?? r.title;
}

async function save(): Promise<void> {
  if (busy.value || numberBad.value) return;
  if (saved.value) {
    emit('done'); // сохранено раньше, форма оставалась открытой ради предупреждения о файле роли
    return;
  }
  const name = role.value.trim();
  const autosendChanged = autosendOk.value !== (props.session.autosend_ok ?? false);
  if (!roleChanged.value && numberValue.value === null && !autosendChanged && !fileChanged.value) {
    emit('cancel');
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    let result;
    if (!roleChanged.value && numberValue.value === null && !fileChanged.value) {
      result = await store.setSessionAutosend(props.session.id, autosendOk.value);
    } else {
      const body: { role: string | null; number?: number; autosend_ok?: boolean; role_file?: string | null } = {
        role: name === '' ? null : name,
      };
      if (name !== '' && numberValue.value !== null) body.number = numberValue.value;
      if (roleChanged.value || autosendChanged) body.autosend_ok = autosendOk.value;
      if (fileChanged.value) body.role_file = roleFile.value.trim() === '' ? null : roleFile.value.trim();
      result = await store.setSessionRole(props.session.id, body);
    }
    if (result.ok && result.warning) {
      warning.value = result.warning;
      saved.value = true;
    } else if (result.ok) emit('done');
    else error.value = result.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <form
    class="work-new-task session-role-form"
    aria-label="Роль сессии"
    @submit.prevent="save"
  >
    <label
      v-if="rolesState.items.length > 0"
      class="work-field"
    >
      <span>Роль из каталога</span>
      <select
        v-model="rolePick"
        class="session-role-pick"
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
    <label class="work-field">
      <span>Роль (пусто — снять)</span>
      <input
        v-model="role"
        type="text"
        class="session-role-input"
        maxlength="100"
      >
    </label>
    <button
      v-if="catalogRole"
      type="button"
      class="link-button session-role-text-link"
      @click="showText = true"
    >
      инструкция роли
    </button>
    <label class="work-field">
      <span>Файл роли (путь в проекте; пусто — снять)</span>
      <input
        v-model="roleFile"
        type="text"
        class="session-role-file"
        maxlength="300"
        placeholder="docs/roles/ROLE.md"
        autocapitalize="off"
        spellcheck="false"
      >
      <small class="session-role-file-hint">MD-файл с инструкцией роли; подставляется в сессию дословно и не теряется при сжатии. Claude Code закрепляет инструкцию при создании сессии; правку файла сессия получит сообщением при следующем запуске, насовсем — в новой сессии. OpenCode читает файл при каждом запуске.</small>
    </label>
    <label class="work-field">
      <span>Номер (пусто — свободный)</span>
      <input
        v-model="number"
        type="text"
        inputmode="numeric"
        class="session-role-number"
        :aria-invalid="numberBad"
      >
    </label>
    <label class="work-field work-field--check">
      <input
        v-model="autosendOk"
        type="checkbox"
        class="session-role-autosend"
      >
      <span>может отвечать в беседы «без подтверждения» своей роли</span>
    </label>
    <p
      v-if="role.trim() !== '' && autosendCount > 0"
      class="session-sub-autosend"
    >
      {{ autosendNote(autosendCount) }}
    </p>
    <p class="session-model-warning">
      Применится со следующего входа: сессия перезапустится, когда закончит ход.
    </p>
    <div class="work-actions">
      <button
        type="submit"
        class="session-role-save"
        :disabled="busy || numberBad"
      >
        {{ saved ? 'Закрыть' : 'Сохранить' }}
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
      v-if="warning !== null"
      class="session-model-warning session-role-file-warning"
      role="status"
    >
      Сохранено. Но {{ warning }} — сессия запустится без файла роли, пока его не будет.
    </p>
    <p
      v-if="error"
      class="work-error"
      role="alert"
    >
      {{ error }}
    </p>
    <RoleTextModal
      v-if="showText && catalogRole"
      :role-key="catalogRole.key"
      :title="catalogRole.title"
      @close="showText = false"
    />
  </form>
</template>
