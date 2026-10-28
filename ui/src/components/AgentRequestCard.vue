<script setup lang="ts">
// Ф41-5 (интерфейс, раздел 6 и 12 спеки): карточка заявки агента. Перенос беседы — «<кто> просит перенести беседу «…» в
// «<проект>»»: «Перенести» / «Нет». Напарник — «<кто> просит напарника: «роль», движок, режим — <задача>»: «Завести» /
// «Изменить и завести» (форма «Новая сессия» с полями заявки и флажком «может отвечать в беседы „без подтверждения“
// своей роли») / «Нет» (с полем причины). Отказ ядра (409) — его текст в карточке, заявка остаётся ждущей. Все поля
// заявки — чужой ввод: только текстовая интерполяция.
import { computed, ref } from 'vue';

import type { AgentRequest } from '../api/types';
import { store } from '../store/instance';
import { moveHeadline, partnerHeadline } from '../util/agentRequests';
import { projectName } from '../util/mapping';
import NewSessionForm from './NewSessionForm.vue';

const props = defineProps<{ request: AgentRequest }>();

const busy = ref(false);
const error = ref<string | null>(null);
const editing = ref(false);
const declining = ref(false);
const note = ref('');

const isMove = computed(() => props.request.kind === 'move_channel');
const headline = computed(() =>
  isMove.value
    ? moveHeadline(props.request, projectName(store.state.projects, props.request.project_id))
    : partnerHeadline(props.request),
);

async function approve(): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    // Ф50-7: оба ключа — всегда, значениями из заявки (`""`, если агент не задал).
    const body = isMove.value
      ? {}
      : { subdir: props.request.payload?.subdir ?? '', role_file: props.request.payload?.role_file ?? '' };
    const result = await store.approveAgentRequest(props.request, body);
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}

async function decline(): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.declineAgentRequest(props.request, note.value);
    if (!result.ok) error.value = result.message;
    else declining.value = false;
  } finally {
    busy.value = false;
  }
}

function onNo(): void {
  if (isMove.value) void decline();
  else declining.value = !declining.value;
}
</script>

<template>
  <div
    class="agent-request-card"
    :data-agent-request-id="request.id"
  >
    <p class="agent-request-line">
      {{ headline }}
    </p>
    <p
      v-if="!isMove && request.payload?.subdir"
      class="agent-request-meta agent-request-subdir"
    >
      папка: {{ request.payload.subdir }}
    </p>
    <p
      v-if="!isMove && request.payload?.role_file"
      class="agent-request-meta agent-request-role-file"
    >
      файл роли: {{ request.payload.role_file }}
    </p>
    <p
      v-if="request.subdir_warning"
      class="agent-request-meta agent-request-warning agent-request-subdir-warning"
    >
      {{ request.subdir_warning }}
    </p>
    <p
      v-if="request.role_file_warning"
      class="agent-request-meta agent-request-warning agent-request-role-file-warning"
    >
      {{ request.role_file_warning }}
    </p>
    <p
      v-if="request.reason"
      class="agent-request-meta"
    >
      Причина: {{ request.reason }}
    </p>
    <div class="agent-request-actions">
      <button
        type="button"
        class="agent-request-approve"
        :disabled="busy"
        @click="approve"
      >
        {{ isMove ? 'Перенести' : 'Завести' }}
      </button>
      <button
        v-if="!isMove"
        type="button"
        class="agent-request-edit"
        :disabled="busy"
        :aria-expanded="editing"
        @click="editing = !editing"
      >
        Изменить и завести
      </button>
      <button
        type="button"
        class="agent-request-decline"
        :disabled="busy"
        @click="onNo"
      >
        Нет
      </button>
    </div>
    <div
      v-if="declining && !isMove"
      class="agent-request-note"
    >
      <input
        v-model="note"
        type="text"
        class="agent-request-note-input"
        maxlength="500"
        placeholder="Почему нет (необязательно)"
        aria-label="Причина отказа"
        @keydown.enter.prevent="decline"
      >
      <button
        type="button"
        class="agent-request-decline-send"
        :disabled="busy"
        @click="decline"
      >
        Отклонить
      </button>
    </div>
    <NewSessionForm
      v-if="editing && !isMove"
      :project-id="request.project_id ?? undefined"
      :request="request"
      @done="editing = false"
      @cancel="editing = false"
    />
    <p
      v-if="error"
      class="agent-request-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
