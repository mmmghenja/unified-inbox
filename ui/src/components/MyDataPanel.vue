<script setup lang="ts">
// Ф82-1 (`docs/architecture.md`): «⋯ → Мои данные» — личные данные агента владельца. Окно поверх (панель
// как «Машины»), ноутбук и телефон. Чипы «Общее» и проекты; поля без значений («••• N зн.»), значение — одним
// запросом по «показать» и только в памяти этого окна (не в хранилище браузера, не в консоль); закрыли окно или сменили
// набор — показанное забыто. У проекта: «из общего» (значение берётся из общего набора), «пусто в этом проекте»
// (пустое значение проекта перекрывает общее), «удалить» убирает ключ из проекта — снова видно общее.
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';

import * as api from '../api/client';
import type { MyDataCatalogKey, MyDataField, MyDataSet } from '../api/types';
import { usePanel } from '../util/popup';

const emit = defineEmits<{ close: [] }>();
const panelRoot = ref<HTMLElement | null>(null);
usePanel(() => panelRoot.value, () => emit('close'));

const sets = ref<MyDataSet[]>([]);
const catalog = ref<MyDataCatalogKey[]>([]);
const valueMax = ref(500);
const scope = ref('common');
const fields = ref<MyDataField[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);
const busy = ref(false);

// Показанные значения: ключ -> значение. Только здесь, чистится при смене набора и закрытии окна.
const shown = reactive(new Map<string, string>());
const editing = ref<string | null>(null);
const draft = ref('');
const confirmDelete = ref<string | null>(null);
const rowError = ref<{ key: string; text: string } | null>(null);

const adding = ref(false);
const addKey = ref('');
const addCustom = ref('');
const addValue = ref('');
const addError = ref<string | null>(null);
const CUSTOM = '__custom__';

const journalOpen = ref(false);
const journal = ref<string[]>([]);
const journalError = ref<string | null>(null);

const isProject = computed(() => scope.value !== 'common');
const current = computed(() => sets.value.find((s) => s.scope === scope.value) ?? null);
// В «добавить» — ключи каталога, которых в этом наборе своих ещё нет (у проекта «из общего» — можно перекрыть).
const addChoices = computed(() => {
  const own = new Set(fields.value.filter((f) => f.source === (isProject.value ? 'project' : 'common')).map((f) => f.key));
  return catalog.value.filter((c) => !own.has(c.key));
});

function errorText(err: unknown): string {
  if (err instanceof api.ApiError) {
    const body = err.body as { detail?: unknown; file?: unknown } | null;
    if (err.status === 409 && body && body.detail === 'broken') {
      return `Файл ${String(body.file)} не читается — хаб его не перезаписывает; поправь файл руками.`;
    }
    if (err.status === 403) return 'Мои данные открываются только из интерфейса хаба.';
    if (err.status === 404) return 'Нет такого поля или проекта.';
    return err.message;
  }
  if (err instanceof api.NetworkError) return 'Нет связи с хабом — попробуй ещё раз.';
  return 'Не удалось — попробуй ещё раз.';
}

function forget(): void {
  shown.clear();
  editing.value = null;
  draft.value = '';
  confirmDelete.value = null;
  rowError.value = null;
}

function whoText(f: MyDataField): string {
  const who = f.by === 'owner_channel' ? 'Владелец' : f.by === 'agent' ? 'агент со слов владельца' : '';
  if (f.ts === null) return who;
  const when = new Date(f.ts).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
  return who ? `${who} · ${when}` : when;
}

function hiddenText(f: MyDataField): string {
  if (!f.filled) return f.source === 'project' ? 'пусто в этом проекте' : 'пусто';
  return `••• ${f.length} зн.`;
}

async function loadFields(): Promise<void> {
  error.value = null;
  try {
    const result = await api.getMyDataFields(scope.value);
    fields.value = result.fields;
  } catch (err) {
    fields.value = [];
    error.value = errorText(err);
  }
}

async function loadOverview(): Promise<void> {
  const result = await api.getMyData();
  sets.value = result.sets;
  catalog.value = result.catalog;
  valueMax.value = result.value_max;
}

async function load(): Promise<void> {
  loading.value = true;
  error.value = null;
  try {
    await loadOverview();
    await loadFields();
  } catch (err) {
    error.value = errorText(err);
  } finally {
    loading.value = false;
  }
}

async function pick(next: string): Promise<void> {
  if (next === scope.value) return;
  scope.value = next;
  forget();
  adding.value = false;
  journal.value = [];
  journalOpen.value = false;
  await loadFields();
}

async function refresh(): Promise<void> {
  try {
    await loadOverview();
  } catch {
    // список наборов — вторично; ошибку покажет следующая загрузка
  }
  await loadFields();
}

async function toggleShow(f: MyDataField): Promise<void> {
  if (shown.has(f.key)) {
    shown.delete(f.key);
    return;
  }
  rowError.value = null;
  try {
    const result = await api.getMyDataValue(scope.value, f.key);
    shown.set(f.key, result.value);
  } catch (err) {
    rowError.value = { key: f.key, text: errorText(err) };
  }
}

async function startEdit(f: MyDataField): Promise<void> {
  rowError.value = null;
  confirmDelete.value = null;
  // В поле правки — текущее значение (его же владелец увидел бы по «показать»); из общего — тоже, как основа.
  if (f.filled && !shown.has(f.key)) {
    try {
      const result = await api.getMyDataValue(scope.value, f.key);
      draft.value = result.value;
    } catch (err) {
      rowError.value = { key: f.key, text: errorText(err) };
      return;
    }
  } else {
    draft.value = shown.get(f.key) ?? '';
  }
  editing.value = f.key;
}

function cancelEdit(): void {
  editing.value = null;
  draft.value = '';
}

async function write(key: string, value: string): Promise<boolean> {
  busy.value = true;
  rowError.value = null;
  try {
    await api.putMyDataValue(scope.value, key, value);
    shown.delete(key);
    await refresh();
    return true;
  } catch (err) {
    rowError.value = { key, text: errorText(err) };
    return false;
  } finally {
    busy.value = false;
  }
}

async function saveEdit(f: MyDataField): Promise<void> {
  if (busy.value) return;
  if (draft.value.trim() === '') {
    rowError.value = { key: f.key, text: isProject.value ? 'Пустое — кнопкой «пусто в этом проекте».' : 'Пустое значение не сохраняется — удали поле.' };
    return;
  }
  if (await write(f.key, draft.value)) cancelEdit();
}

async function makeEmpty(f: MyDataField): Promise<void> {
  if (busy.value) return;
  await write(f.key, '');
}

async function remove(f: MyDataField): Promise<void> {
  if (confirmDelete.value !== f.key) {
    confirmDelete.value = f.key;
    return;
  }
  busy.value = true;
  rowError.value = null;
  try {
    await api.deleteMyDataValue(scope.value, f.key);
    shown.delete(f.key);
    confirmDelete.value = null;
    await refresh();
  } catch (err) {
    rowError.value = { key: f.key, text: errorText(err) };
  } finally {
    busy.value = false;
  }
}

function startAdd(): void {
  adding.value = true;
  addKey.value = addChoices.value[0]?.key ?? CUSTOM;
  addCustom.value = '';
  addValue.value = '';
  addError.value = null;
}

async function saveAdd(): Promise<void> {
  if (busy.value) return;
  const key = addKey.value === CUSTOM ? addCustom.value.trim() : addKey.value;
  if (!/^[a-z0-9_]{1,60}$/.test(key)) {
    addError.value = 'Ключ: латиница в нижнем регистре, цифры и «_», до 60 знаков.';
    return;
  }
  if (addValue.value.trim() === '') {
    addError.value = 'Впиши значение.';
    return;
  }
  busy.value = true;
  addError.value = null;
  try {
    await api.putMyDataValue(scope.value, key, addValue.value);
    adding.value = false;
    addValue.value = '';
    await refresh();
  } catch (err) {
    addError.value = errorText(err);
  } finally {
    busy.value = false;
  }
}

async function onJournalToggle(event: Event): Promise<void> {
  journalOpen.value = (event.target as HTMLDetailsElement).open;
  if (!journalOpen.value) return;
  journalError.value = null;
  try {
    journal.value = (await api.getMyDataJournal(scope.value)).items;
  } catch (err) {
    journalError.value = errorText(err);
  }
}

onMounted(() => {
  void load();
});
onBeforeUnmount(() => {
  forget();
  addValue.value = '';
});
</script>

<template>
  <div
    ref="panelRoot"
    class="access-panel my-data-panel"
    role="dialog"
    aria-label="Мои данные"
  >
    <div class="access-panel-header">
      <strong>Мои данные</strong>
      <button
        type="button"
        class="access-panel-close"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </div>
    <p class="my-data-meta">
      Данные агента для регистраций и форм. Проект — поверх общего: нет поля в проекте — берётся общее.
    </p>
    <div
      class="my-data-chips"
      role="tablist"
    >
      <button
        v-for="s in sets"
        :key="s.scope"
        type="button"
        role="tab"
        class="my-data-chip"
        :class="{ 'my-data-chip--on': s.scope === scope }"
        :aria-selected="s.scope === scope"
        data-test="my-data-chip"
        @click="pick(s.scope)"
      >
        {{ s.title }}<span
          v-if="s.broken"
          class="my-data-broken"
          title="Файл набора не читается"
        >⚠</span><span
          v-else-if="s.count > 0"
          class="my-data-count"
        >{{ s.count }}</span>
      </button>
    </div>
    <p
      v-if="error !== null"
      class="access-error"
      role="alert"
      data-test="my-data-error"
    >
      {{ error }}
    </p>
    <p
      v-if="loading"
      class="my-data-meta"
    >
      Загрузка…
    </p>
    <div
      v-else
      class="my-data-list"
    >
      <div
        v-for="f in fields"
        :key="f.key"
        class="my-data-row"
        data-test="my-data-row"
      >
        <div class="my-data-head">
          <span class="my-data-label">{{ f.label }}</span>
          <span
            v-if="f.label !== f.key"
            class="my-data-meta"
          >{{ f.key }}</span>
          <span
            v-if="f.from_common"
            class="my-data-from-common"
            data-test="my-data-from-common"
          >из общего</span>
        </div>
        <template v-if="editing === f.key">
          <input
            v-model="draft"
            class="my-data-input"
            :maxlength="valueMax"
            :aria-label="`Значение: ${f.label}`"
            autocomplete="off"
            data-test="my-data-edit"
            :disabled="busy"
            @keydown.enter.prevent="saveEdit(f)"
            @keydown.esc.prevent.stop="cancelEdit"
          >
          <div class="my-data-actions">
            <button
              type="button"
              data-test="my-data-save"
              :disabled="busy"
              @click="saveEdit(f)"
            >
              Сохранить{{ f.from_common ? ' в проект' : '' }}
            </button>
            <button
              type="button"
              class="link-button"
              @click="cancelEdit"
            >
              Отмена
            </button>
          </div>
        </template>
        <template v-else>
          <div class="my-data-value">
            <span
              v-if="shown.has(f.key)"
              class="my-data-shown"
              data-test="my-data-shown"
            >{{ shown.get(f.key) }}</span>
            <span
              v-else
              class="my-data-hidden"
              data-test="my-data-hidden"
            >{{ hiddenText(f) }}</span>
          </div>
          <div class="my-data-actions">
            <button
              v-if="f.filled"
              type="button"
              class="link-button"
              data-test="my-data-show"
              @click="toggleShow(f)"
            >
              {{ shown.has(f.key) ? 'скрыть' : 'показать' }}
            </button>
            <button
              type="button"
              class="link-button"
              data-test="my-data-change"
              @click="startEdit(f)"
            >
              изменить
            </button>
            <button
              v-if="isProject && !(f.source === 'project' && !f.filled)"
              type="button"
              class="link-button"
              data-test="my-data-empty"
              title="В этом проекте поле пустое — общее значение не подставляется"
              :disabled="busy"
              @click="makeEmpty(f)"
            >
              пусто в этом проекте
            </button>
            <button
              v-if="f.source === (isProject ? 'project' : 'common')"
              type="button"
              class="link-button my-data-delete"
              data-test="my-data-delete"
              :disabled="busy"
              @click="remove(f)"
            >
              {{ confirmDelete === f.key ? 'точно удалить?' : 'удалить' }}
            </button>
          </div>
        </template>
        <span
          v-if="whoText(f)"
          class="my-data-meta"
        >{{ whoText(f) }}</span>
        <p
          v-if="rowError !== null && rowError.key === f.key"
          class="my-data-row-error"
          role="alert"
          data-test="my-data-row-error"
        >
          {{ rowError.text }}
        </p>
      </div>
      <p
        v-if="fields.length === 0 && error === null"
        class="my-data-meta"
      >
        {{ isProject ? 'В проекте своих полей нет — действует общее.' : 'Полей пока нет.' }}
      </p>
    </div>

    <div
      v-if="!loading && !(current?.broken ?? false)"
      class="my-data-add"
    >
      <button
        v-if="!adding"
        type="button"
        data-test="my-data-add"
        @click="startAdd"
      >
        Добавить поле
      </button>
      <div
        v-else
        class="my-data-add-form"
      >
        <select
          v-model="addKey"
          aria-label="Поле"
          data-test="my-data-add-key"
        >
          <option
            v-for="c in addChoices"
            :key="c.key"
            :value="c.key"
          >
            {{ c.label }}
          </option>
          <option :value="CUSTOM">
            Свой ключ…
          </option>
        </select>
        <input
          v-if="addKey === CUSTOM"
          v-model="addCustom"
          placeholder="ключ латиницей, например shop_login"
          maxlength="60"
          autocomplete="off"
          aria-label="Свой ключ"
          data-test="my-data-add-custom"
        >
        <input
          v-model="addValue"
          placeholder="значение"
          :maxlength="valueMax"
          autocomplete="off"
          aria-label="Значение"
          data-test="my-data-add-value"
          @keydown.enter.prevent="saveAdd"
        >
        <div class="my-data-actions">
          <button
            type="button"
            data-test="my-data-add-save"
            :disabled="busy"
            @click="saveAdd"
          >
            Сохранить
          </button>
          <button
            type="button"
            class="link-button"
            @click="adding = false"
          >
            Отмена
          </button>
        </div>
        <p
          v-if="addError !== null"
          class="my-data-row-error"
          role="alert"
          data-test="my-data-add-error"
        >
          {{ addError }}
        </p>
      </div>
    </div>

    <details
      class="my-data-journal"
      data-test="my-data-journal"
      :open="journalOpen"
      @toggle="onJournalToggle"
    >
      <summary>Журнал регистраций</summary>
      <p
        v-if="journalError !== null"
        class="my-data-row-error"
      >
        {{ journalError }}
      </p>
      <ul v-else-if="journal.length > 0">
        <li
          v-for="(line, i) in journal"
          :key="i"
          data-test="my-data-journal-line"
        >
          {{ line }}
        </li>
      </ul>
      <p
        v-else
        class="my-data-meta"
      >
        Записей нет.
      </p>
    </details>
  </div>
</template>
