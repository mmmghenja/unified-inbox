<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import type { Channel } from '../api/types';
import { channelsOfProject } from '../store/selectors';
import { store } from '../store/instance';
import { splitByAttention } from '../util/channelGroup';
import { MOVE_MAX, moveTargetOptions, targetOf } from '../util/mapping';
import { readUiString, writeUiString } from '../util/uiPrefs';
import {
  applyStageFilter,
  parseStageFilter,
  stageFilterPrefKey,
  stageGroups,
  type StageFilter,
} from '../util/stages';
import * as api from '../api/client';
import { numberQuery } from '../util/draftNumber';
import ChannelGroups from './ChannelGroups.vue';
import ChannelNo from './ChannelNo.vue';
import NumberSearch from './NumberSearch.vue';
import DayControl from './DayControl.vue';
import StagesBlock from './StagesBlock.vue';

// F4 приёмки 6b: список вычисляется из sync.channelsById на КАЖДОЕ обращение (а не хранится отдельной
// REST-копией) — так изменения канала из /sync и новые каналы видны сразу.
const channels = computed(() => {
  const projectId = store.state.currentProjectId;
  if (projectId === null) return [];
  return channelsOfProject(store.state.sync, projectId);
});

function primaryUnread(channel: Channel): number {
  return channel.unread_top ?? channel.unread;
}

// Ф8-6 C3: «Реагирую» сверху, «К сведению» ниже — свёрнута по умолчанию, со счётчиком непрочитанного. Группы
// «Чаты / Почта / Задачи» — внутри каждой (`ChannelGroups.vue`). Открытая беседа из свёрнутой группы её
// разворачивает (иначе выбранная беседа пропала бы из списка).
// Ф34-1: фильтр по стадии (`all` / `none` / `s:<ключ>`) помнится по проекту; стадий в проекте нет — фильтра нет.
const groups = computed(() => stageGroups(channels.value));
const filterRaw = ref<string | null>(null);
const filterPref = computed(() => {
  const projectId = store.state.currentProjectId;
  return projectId === null ? null : stageFilterPrefKey(projectId);
});
watch(
  filterPref,
  (key) => {
    filterRaw.value = key === null ? null : readUiString(key);
  },
  { immediate: true },
);
const stageFilter = computed<StageFilter>(() => parseStageFilter(filterRaw.value, groups.value));

function onStageFilter(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  filterRaw.value = value;
  if (filterPref.value !== null) writeUiString(filterPref.value, value);
}

const split = computed(() =>
  splitByAttention(applyStageFilter(channels.value.filter((c) => !c.archived), stageFilter.value)),
);
const infoUnread = computed(() => split.value.info.reduce((sum, c) => sum + primaryUnread(c), 0));
const infoOpenByUser = ref(false);
const infoOpen = computed(
  () => infoOpenByUser.value || split.value.info.some((c) => c.id === store.state.currentChannelId),
);

// Поиск беседы по номеру (`#20` / `20`): находит беседу в любом проекте и в архиве — из стора, иначе `GET /channels/{id}`.
const numberRaw = ref('');
const numberHit = ref<Channel | null>(null);
const numberMiss = ref(false);
let numberSeq = 0;
watch(numberRaw, async (raw) => {
  const seq = ++numberSeq;
  const digits = numberQuery(raw);
  numberHit.value = null;
  numberMiss.value = false;
  if (digits === '' || !Number.isSafeInteger(Number(digits))) return;
  const id = Number(digits);
  const known = store.state.sync.channelsById[id];
  if (known !== undefined) {
    numberHit.value = known;
    return;
  }
  try {
    const found = await api.getChannel(id);
    if (seq !== numberSeq) return;
    if (found !== null && typeof found === 'object') numberHit.value = found;
    else numberMiss.value = true;
  } catch {
    if (seq === numberSeq) numberMiss.value = true;
  }
});

function openNumberHit(): void {
  const hit = numberHit.value;
  if (hit === null) return;
  void store.openChannelInProject(hit.id);
}

// Ф14-2 D1: кнопка «Новое сообщение» ушла отсюда в верхнюю полосу (`NewMessageButton.vue`).

// Ф14-1 C2: выбор бесед и перенос пачкой. «Все» — все неархивные беседы списка, включая свёрнутую «К сведению»;
// больше `MOVE_MAX` — «Перенести» неактивна. Успех — «Перенесено N» на 5 с и выход из режима; ошибка — текстом,
// выбор цел. Смена вкладки — выход из режима.
const MOVE_NOTICE_MS = 5_000;
const selecting = ref(false);
const selected = ref<Set<number>>(new Set());
const moveValue = ref('');
const moveBusy = ref(false);
const moveError = ref<string | null>(null);
const moveNotice = ref<string | null>(null);
let noticeTimer: ReturnType<typeof setTimeout> | null = null;

const listIds = computed(() => [...split.value.react, ...split.value.info].map((c) => c.id));
const selectedIds = computed(() => listIds.value.filter((id) => selected.value.has(id)));
const tooMany = computed(() => selectedIds.value.length > MOVE_MAX);
const moveOptions = computed(() => moveTargetOptions(store.state.projects));
const canMove = computed(
  () => !moveBusy.value && selectedIds.value.length > 0 && !tooMany.value && targetOf(moveValue.value) !== undefined,
);

function startSelect(): void {
  selecting.value = true;
  selected.value = new Set();
  moveValue.value = '';
  moveError.value = null;
}

function cancelSelect(): void {
  selecting.value = false;
  selected.value = new Set();
  moveValue.value = '';
  moveError.value = null;
}

function toggle(channelId: number): void {
  const next = new Set(selected.value);
  if (next.has(channelId)) next.delete(channelId);
  else next.add(channelId);
  selected.value = next;
}

function selectAll(): void {
  selected.value = new Set(listIds.value);
}

async function doMove(): Promise<void> {
  const target = targetOf(moveValue.value);
  if (!canMove.value || target === undefined) return;
  moveBusy.value = true;
  moveError.value = null;
  try {
    const moved = await store.moveChannels(selectedIds.value, target);
    cancelSelect();
    moveNotice.value = `Перенесено ${moved}`;
    if (noticeTimer !== null) clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => {
      moveNotice.value = null;
      noticeTimer = null;
    }, MOVE_NOTICE_MS);
  } catch (err) {
    moveError.value = err instanceof Error && err.message ? `Не удалось перенести: ${err.message}` : 'Не удалось перенести.';
  } finally {
    moveBusy.value = false;
  }
}

watch(
  () => [store.state.currentProjectId, store.state.homeOpen],
  () => cancelSelect(),
);

onBeforeUnmount(() => {
  if (noticeTimer !== null) clearTimeout(noticeTimer);
});
</script>

<template>
  <nav
    class="column column-channels"
    aria-label="Каналы"
  >
    <DayControl
      v-if="store.state.currentProjectId !== null && store.state.currentProjectId >= 1"
      :project-id="store.state.currentProjectId"
      class="channel-day"
    />
    <StagesBlock :groups="groups" />
    <NumberSearch
      v-model="numberRaw"
      placeholder="номер беседы"
      label="Найти беседу по номеру"
    />
    <p
      v-if="numberMiss"
      class="empty-hint"
    >
      Беседы с таким номером нет.
    </p>
    <button
      v-if="numberHit !== null"
      type="button"
      class="channel-button channel-no-result"
      data-test="channel-no-result"
      @click="openNumberHit"
    >
      <span class="channel-title">{{ numberHit.title ?? '(без названия)' }}</span>
      <ChannelNo :id="numberHit.id" />
      <span
        v-if="numberHit.archived"
        class="badge badge-quiet"
      >архив</span>
    </button>
    <div
      v-if="groups.length > 0"
      class="stage-filter"
    >
      <select
        class="stage-filter-select"
        aria-label="Фильтр по стадии"
        :value="stageFilter"
        @change="onStageFilter"
      >
        <option value="all">
          Стадия ▾ все
        </option>
        <option value="none">
          без стадии
        </option>
        <option
          v-for="group in groups"
          :key="group.key"
          :value="`s:${group.key}`"
        >
          {{ group.label }} ({{ group.count }})
        </option>
      </select>
    </div>
    <div
      v-if="!selecting && listIds.length > 0"
      class="channel-select-start"
    >
      <button
        type="button"
        class="channel-select-button"
        @click="startSelect"
      >
        Выбрать
      </button>
    </div>
    <p
      v-if="moveNotice !== null"
      class="move-notice"
      role="status"
    >
      {{ moveNotice }}
    </p>
    <div
      v-if="selecting"
      class="channel-select-bar"
      aria-label="Выбранные беседы"
    >
      <span class="channel-select-count">Выбрано: {{ selectedIds.length }}</span>
      <button
        type="button"
        class="channel-select-all"
        @click="selectAll"
      >
        Все
      </button>
      <select
        v-model="moveValue"
        class="move-target-select"
        aria-label="Перенести в"
      >
        <option value="">
          Перенести в…
        </option>
        <option
          v-for="option in moveOptions"
          :key="option.value"
          :value="option.value"
        >
          {{ option.label }}
        </option>
      </select>
      <button
        type="button"
        class="move-button"
        :disabled="!canMove"
        :title="tooMany ? `За раз — не больше ${MOVE_MAX} бесед` : undefined"
        @click="doMove"
      >
        Перенести
      </button>
      <button
        type="button"
        class="channel-select-cancel"
        @click="cancelSelect"
      >
        Отмена
      </button>
      <p
        v-if="tooMany"
        class="move-hint"
      >
        За раз — не больше {{ MOVE_MAX }} бесед.
      </p>
      <p
        v-if="moveError !== null"
        class="move-error"
        role="alert"
      >
        {{ moveError }}
      </p>
    </div>
    <section
      v-if="split.react.length > 0"
      class="attention-group attention-group--react"
    >
      <h2 class="attention-group-title">
        Реагирую
      </h2>
      <ChannelGroups
        :channels="split.react"
        :selecting="selecting"
        :selected="selected"
        @toggle="toggle"
      />
    </section>
    <section
      v-if="split.info.length > 0"
      class="attention-group attention-group--info"
    >
      <button
        type="button"
        class="attention-group-toggle"
        :aria-expanded="infoOpen"
        @click="infoOpenByUser = !infoOpenByUser"
      >
        <span>{{ infoOpen ? '▾' : '▸' }} К сведению</span>
        <span
          v-if="infoUnread > 0"
          class="badge badge-quiet"
        >{{ infoUnread }}</span>
      </button>
      <ChannelGroups
        v-if="infoOpen"
        :channels="split.info"
        :selecting="selecting"
        :selected="selected"
        @toggle="toggle"
      />
    </section>
    <p
      v-if="channels.length === 0 || (listIds.length === 0 && stageFilter !== 'all')"
      class="empty-hint"
    >
      {{ channels.length === 0 ? 'В проекте пока нет каналов.' : 'Нет бесед с такой стадией.' }}
    </p>
  </nav>
</template>
