<script setup lang="ts">
// Ф62-6 (раздел G): «Новая группа» WhatsApp — режим формы «Новое сообщение». Название (до 25 символов, счётчик),
// аккаунт WhatsApp, проект («По правилам» или проект), участники с поиском «Кому» (люди и лички WhatsApp,
// номер вручную). «Создать» -> `POST /group-ops/ui` (сразу, без карточки) -> строка «создаётся…» -> группа
// создана: беседа открыта; не вышло: причина. Названия и имена — чужой текст, только интерполяция.
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import { ApiError } from '../api/client';
import type { Account, GroupOp } from '../api/types';
import { store } from '../store/instance';
import { GROUP_TITLE_MAX, groupOpErrorText, groupOpOutcomeLines, groupOpProgress, type PickedPerson } from '../util/groupOps';
import { layout } from '../util/layout';
import { accountLabel } from '../util/newMessage';
import GroupPeoplePicker from './GroupPeoplePicker.vue';

const props = defineProps<{ projectId: number | null; accounts: Account[] }>();
const emit = defineEmits<{ done: [channelId: number]; cancel: []; mode: [mode: 'message' | 'group'] }>();

const POLL_MS = 3000;

const title = ref('');
const people = ref<PickedPerson[]>([]);
const chosenAccount = ref<number | null>(props.accounts[0]?.id ?? null);
const chosenProject = ref<string>(props.projectId !== null && props.projectId > 0 ? String(props.projectId) : '');
const busy = ref(false);
const error = ref<string | null>(null);
const opId = ref<number | null>(null);

const phone = computed(() => layout.mode === 'phone');
const projects = computed(() => store.state.projects.filter((p) => p.id > 0));
const op = computed<GroupOp | null>(() => (opId.value === null ? null : (store.state.groupOps.items[opId.value] ?? null)));
const titleTrim = computed(() => title.value.trim());
const canSubmit = computed(
  () => !busy.value && opId.value === null && chosenAccount.value !== null && titleTrim.value !== '' && titleTrim.value.length <= GROUP_TITLE_MAX && people.value.length > 0,
);

watch(chosenAccount, () => {
  people.value = [];
});

let poll: ReturnType<typeof setInterval> | null = null;
let opened = false;

function stopPoll(): void {
  if (poll !== null) clearInterval(poll);
  poll = null;
}

function startPoll(id: number): void {
  stopPoll();
  poll = setInterval(() => {
    void store.refreshGroupOp(id).catch(() => undefined);
  }, POLL_MS);
}

// Исход заявки: `done` — открыть беседу группы; `failed`/`declined` — причина, форма снова доступна.
watch(op, async (current) => {
  if (current === null || opened) return;
  if (current.status === 'done') {
    stopPoll();
    if (current.channel_id === null) {
      error.value = 'Группа создана, но беседа ещё не появилась — найдите её в списке бесед.';
      return;
    }
    opened = true;
    const channelId = current.channel_id;
    await store.openRecipientChannel(current.project_id, channelId).catch(() => undefined);
    emit('done', channelId);
  } else if (current.status === 'failed' || current.status === 'declined') {
    stopPoll();
    error.value = current.status === 'declined' ? 'Создание отменено.' : `Группа не создана: ${groupOpErrorText(current.error)}.`;
    opId.value = null;
  }
});

async function submit(): Promise<void> {
  if (!canSubmit.value || chosenAccount.value === null) return;
  busy.value = true;
  error.value = null;
  try {
    const created = await store.submitGroupOp({
      kind: 'create',
      accountId: chosenAccount.value,
      projectId: chosenProject.value === '' ? null : Number(chosenProject.value),
      title: titleTrim.value,
      participants: people.value.map((p) => p.value),
    });
    opId.value = created.id;
    startPoll(created.id);
  } catch (err) {
    error.value =
      err instanceof ApiError && [403, 409, 422, 429].includes(err.status) && err.message
        ? err.message
        : 'Группа не создана — проверьте соединение и попробуйте ещё раз.';
  } finally {
    busy.value = false;
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault();
    emit('cancel');
  }
}

onBeforeUnmount(stopPoll);
</script>

<template>
  <form
    class="new-message-form new-group-form"
    :class="{ 'new-message-form--phone': phone }"
    role="dialog"
    aria-label="Новая группа"
    @submit.prevent="submit"
    @keydown="onKeydown"
  >
    <div class="group-mode-switch">
      <button
        type="button"
        aria-pressed="false"
        @click="emit('mode', 'message')"
      >
        Сообщение
      </button>
      <button
        type="button"
        aria-pressed="true"
      >
        Новая группа
      </button>
    </div>
    <h3 class="new-message-title">
      Новая группа WhatsApp
    </h3>
    <label class="new-message-field">
      <span>Название</span>
      <input
        v-model="title"
        type="text"
        class="new-group-title"
        :maxlength="GROUP_TITLE_MAX"
        :disabled="opId !== null"
        required
      >
      <span class="group-title-count">{{ title.length }} / {{ GROUP_TITLE_MAX }}</span>
    </label>
    <label
      v-if="accounts.length > 1"
      class="new-message-field"
    >
      <span>Откуда</span>
      <select
        v-model="chosenAccount"
        class="new-message-account"
        :disabled="opId !== null"
      >
        <option
          v-for="item in accounts"
          :key="item.id"
          :value="item.id"
        >
          {{ accountLabel(item) }}
        </option>
      </select>
    </label>
    <div class="new-message-field">
      <span>Участники</span>
      <GroupPeoplePicker
        v-model="people"
        :account-id="chosenAccount"
      />
    </div>
    <label class="new-message-field">
      <span>Проект</span>
      <select
        v-model="chosenProject"
        class="new-message-project"
        :disabled="opId !== null"
      >
        <option value="">
          По правилам
        </option>
        <option
          v-for="p in projects"
          :key="p.id"
          :value="String(p.id)"
        >
          {{ p.name ?? '(без названия)' }}
        </option>
      </select>
    </label>
    <div class="new-message-actions">
      <button
        type="submit"
        class="new-message-submit"
        :disabled="!canSubmit"
      >
        Создать
      </button>
      <button
        type="button"
        class="new-message-cancel"
        @click="emit('cancel')"
      >
        Отмена
      </button>
    </div>
    <p
      v-if="op !== null && (op.status === 'pending' || op.status === 'approved')"
      class="new-message-hint new-group-progress"
    >
      {{ groupOpProgress(op) }}
    </p>
    <ul
      v-if="op !== null && groupOpOutcomeLines(op).length > 0"
      class="group-op-result"
    >
      <li
        v-for="line in groupOpOutcomeLines(op)"
        :key="line"
      >
        {{ line }}
      </li>
    </ul>
    <p
      v-if="error"
      class="new-message-error"
      role="alert"
    >
      {{ error }}
    </p>
  </form>
</template>
