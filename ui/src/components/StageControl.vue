<script setup lang="ts">
// Ф34-1 A6: стадия беседы в шапке — «Стадия ▾» (стадии проекта, «Новая…», «Снять») и заметка рядом (правка по
// клику). Тексты стадии и заметки — произвольные данные: только текстовая интерполяция.
import { computed, nextTick, ref, watch } from 'vue';

import { UNSORTED_PROJECT_ID } from '../api/types';
import { store } from '../store/instance';
import { channelsOfProject } from '../store/selectors';
import { hasStage, STAGE_MAX, STAGE_NOTE_MAX, stageGroups, stageKey } from '../util/stages';

const props = defineProps<{ channelId: number }>();

const NEW = '__new__';
const CLEAR = '__clear__';

const channel = computed(() => store.state.sync.channelsById[props.channelId] ?? null);
const groups = computed(() => {
  if (channel.value === null) return [];
  // Беседа без проекта («Неразобранное») — стадии берём среди таких же.
  const projectId = channel.value.project_id ?? UNSORTED_PROJECT_ID;
  return stageGroups(channelsOfProject(store.state.sync, projectId));
});
const current = computed(() => (channel.value !== null && hasStage(channel.value) ? channel.value : null));
const selectValue = computed(() => (current.value === null ? '' : stageKey(current.value.stage)));
// Стадия беседы может не попасть в `groups` (беседа в архиве) — добавим её отдельной строкой.
const options = computed(() => {
  const list = groups.value.map((g) => ({ key: g.key, label: g.label, count: g.count }));
  const cur = current.value;
  if (cur !== null && !list.some((o) => o.key === stageKey(cur.stage))) {
    list.push({ key: stageKey(cur.stage), label: (cur.stage ?? '').trim(), count: 1 });
  }
  return list;
});

const error = ref<string | null>(null);
const busy = ref(false);
const selectKey = ref(0);
const mode = ref<'idle' | 'new' | 'note'>('idle');
const draft = ref('');
const inputEl = ref<HTMLInputElement | null>(null);

function fail(err: unknown): void {
  error.value = err instanceof Error && err.message ? err.message : 'Не удалось сохранить стадию.';
  // Перерисовать `<select>`: значение из стора не менялось, а DOM уже показывает выбранное.
  selectKey.value += 1;
}

async function run(action: () => Promise<void>): Promise<boolean> {
  if (busy.value) return false;
  busy.value = true;
  error.value = null;
  try {
    await action();
    return true;
  } catch (err) {
    fail(err);
    return false;
  } finally {
    busy.value = false;
  }
}

async function focusInput(): Promise<void> {
  await nextTick();
  inputEl.value?.focus();
}

async function onSelect(event: Event): Promise<void> {
  const value = (event.target as HTMLSelectElement).value;
  error.value = null;
  if (value === NEW) {
    mode.value = 'new';
    draft.value = '';
    selectKey.value += 1;
    await focusInput();
    return;
  }
  mode.value = 'idle';
  if (value === CLEAR) {
    await run(() => store.setChannelStage(props.channelId, null));
    selectKey.value += 1;
    return;
  }
  const option = options.value.find((o) => o.key === value);
  if (option === undefined || value === selectValue.value) {
    selectKey.value += 1;
    return;
  }
  await run(() => store.setChannelStage(props.channelId, option.label));
}

async function submit(): Promise<void> {
  const text = draft.value.trim();
  if (mode.value === 'new') {
    if (text === '') {
      mode.value = 'idle';
      return;
    }
    if (await run(() => store.setChannelStage(props.channelId, text))) mode.value = 'idle';
  } else if (mode.value === 'note') {
    if (await run(() => store.setChannelStageNote(props.channelId, text === '' ? null : text))) mode.value = 'idle';
  }
}

function cancel(): void {
  mode.value = 'idle';
  error.value = null;
}

async function editNote(): Promise<void> {
  draft.value = current.value?.stage_note ?? '';
  mode.value = 'note';
  error.value = null;
  await focusInput();
}

watch(
  () => props.channelId,
  () => {
    mode.value = 'idle';
    error.value = null;
  },
);
</script>

<template>
  <span class="stage-control">
    <select
      :key="selectKey"
      class="stage-select"
      aria-label="Стадия беседы"
      :value="selectValue"
      :disabled="busy"
      @change="onSelect"
    >
      <option value="">
        {{ current === null ? 'Стадия ▾' : 'Без стадии' }}
      </option>
      <option
        v-for="option in options"
        :key="option.key"
        :value="option.key"
      >
        {{ option.label }} ({{ option.count }})
      </option>
      <option :value="NEW">
        Новая…
      </option>
      <option
        v-if="current !== null"
        :value="CLEAR"
      >
        Снять
      </option>
    </select>
    <form
      v-if="mode !== 'idle'"
      class="stage-form"
      @submit.prevent="submit"
    >
      <input
        ref="inputEl"
        v-model="draft"
        class="stage-input"
        type="text"
        :maxlength="mode === 'new' ? STAGE_MAX : STAGE_NOTE_MAX"
        :aria-label="mode === 'new' ? 'Новая стадия' : 'Заметка к стадии'"
        :placeholder="mode === 'new' ? 'Название стадии' : 'Заметка (например, догон до 07.10)'"
        @keydown.esc.prevent="cancel"
      >
      <button
        type="submit"
        class="stage-save"
        :disabled="busy"
      >
        Сохранить
      </button>
      <button
        type="button"
        class="stage-cancel"
        @click="cancel"
      >
        Отмена
      </button>
    </form>
    <button
      v-else-if="current !== null"
      type="button"
      class="stage-note"
      :title="current.stage_note ? 'Править заметку' : 'Добавить заметку'"
      @click="editNote"
    >
      {{ current.stage_note || '+ заметка' }}
    </button>
    <span
      v-if="error !== null"
      class="map-error stage-error"
      role="alert"
    >{{ error }}</span>
  </span>
</template>
