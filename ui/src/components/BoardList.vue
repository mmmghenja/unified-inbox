<script setup lang="ts">
// Ф41-6a (2.3): список слева. «Новая сессия», «Оркестратор хаба», по проектам — сессии хаба, живые внешние сессии и
// «беседы ждут»; внизу «Неразобранное», скрытые проекты и «Переписка агентов». Строка — переключатель столбца на
// доске. Свёрнут — узкая полоса: «+» и кружки проектов с числом ждущего. Названия и роли — чужой ввод: только текстом.
import { computed, ref } from 'vue';

import { UNSORTED_PROJECT_ID } from '../api/types';
import { boardState, hideOfflineLine, hideOfflineLines, hideProject, itemKey, openColumn, pickSession, setView, setViewProject, showProject, toggleList, sameItem, type ColumnItem } from '../store/board';
import { store } from '../store/instance';
import { buildAgentLines } from '../store/peerMsgs';
import { buildBoardList, hiddenNames } from '../util/boardList';
import { safeColor } from '../util/color';
import { subdirLabel } from '../util/sessions';
import { buildWaiting } from '../util/waiting';
import { usePopup } from '../util/popup';
import { readUiFlag, writeUiFlag } from '../util/uiPrefs';
import { waitingInputOf } from '../util/waitInput';
import NewSessionForm from './NewSessionForm.vue';

const props = defineProps<{ nowMs?: number }>();

const layout = computed(() => boardState.layout);
const showNew = ref(false);
const menuFor = ref<number | null>(null);
// Ф80-1 A2: «⋯» проекта — Esc, нажатие мимо и открытие другого меню закрывают его (общий механизм `util/popup`).
const listEl = ref<HTMLElement | null>(null);
const menuOpen = computed({
  get: () => menuFor.value !== null,
  set: (v: boolean) => {
    if (!v) menuFor.value = null;
  },
});
usePopup({
  open: menuOpen,
  button: () => listEl.value?.querySelector<HTMLElement>('.board-group-menu[aria-expanded="true"]'),
  panel: () => listEl.value?.querySelector<HTMLElement>('.board-group-popup'),
  place: false,
});

const waiting = computed(() =>
  buildWaiting(waitingInputOf(store)),
);

const groups = computed(() =>
  buildBoardList({
    projects: store.state.projects,
    sessions: Object.values(store.state.sessions.items),
    pending: store.state.sessions.pending,
    agentLines: buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs),
    offline: boardState.offline,
    hiddenLines: layout.value.hidden_lines,
    home: store.state.home,
    waiting: waiting.value,
    hiddenProjects: layout.value.hidden_projects,
    nowMs: props.nowMs ?? Date.now(),
  }),
);
const projectGroups = computed(() => groups.value.filter((g) => g.id !== UNSORTED_PROJECT_ID));
const unsorted = computed(() => groups.value.find((g) => g.id === UNSORTED_PROJECT_ID) ?? null);
const hidden = computed(() => hiddenNames(store.state.projects, layout.value.hidden_projects));
const agentItem: ColumnItem = { kind: 'owner_channel' };

function isOpen(item: ColumnItem): boolean {
  return layout.value.view === 'own' && layout.value.open.some((o) => sameItem(o, item));
}

// Ф47-1: клик по сессии зависит от вида. Свой набор — столбец справа; «По проектам» — выбор в столбце проекта;
// «Один проект» — переключение на её проект и прокрутка к её столбцу.
function pickRow(projectId: number, item: ColumnItem): void {
  if (layout.value.view === 'projects') {
    pickSession(projectId, item);
  } else if (layout.value.view === 'project') {
    setViewProject(projectId);
    boardState.focusKey = itemKey(item);
    boardState.focusSeq += 1;
  } else {
    openColumn(item);
  }
}

// Окно «Агент» — только столбец своего набора: из других видов возвращает на «Свой набор».
function pickAgent(): void {
  setView('own');
  openColumn(agentItem);
}

function openProjectTab(id: number): void {
  void store.openProject(id);
}

function openChannel(projectId: number | null, channelId: number): void {
  if (layout.value.view !== 'own') {
    void store.openChannelOverlay(channelId);
    return;
  }
  void store.openChannelAt(projectId, channelId, {}, { column: true });
}

function pickHide(id: number): void {
  menuFor.value = null;
  hideProject(id);
}

function removeOffline(key: string): void {
  const line = boardState.offline.find((l) => l.key === key);
  if (line !== undefined) hideOfflineLine(line);
}

function pickHideOffline(id: number): void {
  menuFor.value = null;
  hideOfflineLines(id);
}

/** В группе есть линии «не в сети» — в меню проекта виден пункт «Убрать „не в сети“». */
function hasOffline(group: { agents: { offline?: boolean }[] }): boolean {
  return group.agents.some((a) => a.offline === true);
}

// «беседы ждут» проекта по умолчанию свёрнуты (владелец 22.10); раскрытие помнится по проекту.
const chatsOpen = ref<Record<number, boolean>>({});

function chatsShown(id: number): boolean {
  return chatsOpen.value[id] ?? readUiFlag(`hub.board.chats.${id}`) ?? false;
}

function toggleChats(id: number): void {
  const next = !chatsShown(id);
  chatsOpen.value = { ...chatsOpen.value, [id]: next };
  writeUiFlag(`hub.board.chats.${id}`, next);
}

function projectColor(id: number): string | null {
  return store.state.projects.find((p) => p.id === id)?.color ?? null;
}
</script>

<template>
  <aside
    v-if="layout.list_collapsed"
    class="board-list board-list--collapsed"
    aria-label="Список"
  >
    <button
      type="button"
      class="board-list-new"
      title="Новая сессия"
      aria-label="Новая сессия"
      @click="toggleList"
    >
      +
    </button>
    <button
      v-for="group in groups"
      :key="group.id"
      type="button"
      class="board-list-dot"
      :title="group.name"
      :aria-label="group.name"
      @click="toggleList"
    >
      <span
        class="color-dot"
        :style="{ backgroundColor: safeColor(projectColor(group.id)) }"
      />
      <span
        v-if="group.waitCount > 0"
        class="badge badge-react"
      >{{ group.waitCount }}</span>
    </button>
  </aside>
  <aside
    v-else
    ref="listEl"
    class="board-list"
    aria-label="Список"
  >
    <button
      type="button"
      class="board-list-new"
      :aria-expanded="showNew"
      @click="showNew = !showNew"
    >
      Новая сессия
    </button>
    <NewSessionForm
      v-if="showNew"
      @done="showNew = false"
      @cancel="showNew = false"
    />
    <button
      type="button"
      class="board-row board-row--owner"
      :aria-pressed="isOpen(agentItem)"
      @click="pickAgent"
    >
      Агент · все проекты
      <span
        v-if="store.state.owner_channel.items.length > 0"
        class="badge badge-react"
      >{{ store.state.owner_channel.items.length }}</span>
    </button>

    <section
      v-for="group in projectGroups"
      :key="group.id"
      class="board-group"
      :data-project-id="group.id"
    >
      <header class="board-group-head">
        <button
          type="button"
          class="board-group-name"
          @click="openProjectTab(group.id)"
        >
          <span
            class="color-dot"
            :style="{ backgroundColor: safeColor(group.color) }"
          />
          {{ group.name }}
        </button>
        <span
          v-if="group.waitCount > 0"
          class="badge badge-react"
        >{{ group.waitCount }}</span>
        <button
          type="button"
          class="board-group-menu"
          aria-label="Действия с проектом"
          :aria-expanded="menuFor === group.id"
          @click="menuFor = menuFor === group.id ? null : group.id"
        >
          ⋯
        </button>
        <span
          v-if="menuFor === group.id"
          class="board-group-popup"
        >
          <button
            type="button"
            class="board-group-hide"
            @click="pickHide(group.id)"
          >
            Скрыть проект
          </button>
          <button
            v-if="hasOffline(group)"
            type="button"
            class="board-group-hide board-group-hide-offline"
            @click="pickHideOffline(group.id)"
          >
            Убрать «не в сети»
          </button>
        </span>
      </header>
      <button
        v-for="row in group.sessions"
        :key="`s${row.id}`"
        type="button"
        class="board-row board-row--session"
        :aria-pressed="isOpen(row.item)"
        :data-session-id="row.id"
        @click="pickRow(group.id, row.item)"
      >
        <span
          class="board-dot"
          :class="`board-dot--${row.dot}`"
        />
        <span class="board-row-main">
          <span class="board-row-top">
            <span class="board-row-name">{{ row.name }}</span>
            <span
              v-if="row.subdir"
              class="board-row-sub board-row-subdir"
              :title="row.subdir"
            >{{ subdirLabel(row.subdir) }}</span>
            <span class="board-row-sub">{{ row.sub }}</span>
            <span
              v-if="row.auto"
              class="board-row-sub board-row-auto"
            >авто</span>
            <span
              v-if="row.cacheWarn"
              class="board-row-sub board-row-cache"
            >{{ row.cacheWarn }}</span>
          </span>
          <span
            class="board-row-engine"
            :title="row.engineModel"
          >{{ row.engineModel }}</span>
        </span>
      </button>
      <div
        v-for="row in group.agents"
        :key="`a${row.key}`"
        :class="{ 'board-row-wrap': row.offline }"
      >
        <button
          type="button"
          class="board-row board-row--agent"
          :aria-pressed="isOpen(row.item)"
          :data-agent-key="row.key"
          @click="pickRow(group.id, row.item)"
        >
          <span
            class="board-dot"
            :class="`board-dot--${row.dot}`"
          />
          <span class="board-row-name">{{ row.name }}</span>
          <span class="board-row-sub">{{ row.sub }}</span>
        </button>
        <button
          v-if="row.offline"
          type="button"
          class="board-row-remove"
          title="Убрать из списка"
          aria-label="Убрать из списка"
          @click.stop="removeOffline(row.key)"
        >
          ×
        </button>
      </div>
      <template v-if="group.channels.length > 0">
        <button
          type="button"
          class="board-group-chats"
          :aria-expanded="chatsShown(group.id)"
          @click="toggleChats(group.id)"
        >
          {{ chatsShown(group.id) ? '▾' : '▸' }} беседы ждут · {{ group.channels.length }}
        </button>
        <button
          v-for="row in chatsShown(group.id) ? group.channels : []"
          :key="`c${row.channelId}`"
          type="button"
          class="board-row board-row--channel"
          :data-channel-id="row.channelId"
          @click="openChannel(row.projectId, row.channelId)"
        >
          <span class="board-row-name">{{ row.title }}</span>
          <span class="board-row-sub">{{ row.sub }}</span>
        </button>
      </template>
    </section>

    <section
      v-if="unsorted"
      class="board-group board-group--unsorted"
    >
      <header class="board-group-head">
        <span class="board-group-name">Неразобранное</span>
        <span
          v-if="unsorted.waitCount > 0"
          class="badge badge-react"
        >{{ unsorted.waitCount }}</span>
      </header>
      <button
        v-for="row in unsorted.channels"
        :key="`c${row.channelId}`"
        type="button"
        class="board-row board-row--channel"
        :data-channel-id="row.channelId"
        @click="openChannel(row.projectId, row.channelId)"
      >
        <span class="board-row-name">{{ row.title }}</span>
        <span class="board-row-sub">{{ row.sub }}</span>
      </button>
    </section>

    <p
      v-if="hidden.length > 0"
      class="board-hidden"
    >
      Скрыты: {{ hidden.map((h) => h.name).join(', ') }} ·
      <button
        v-for="h in hidden"
        :key="h.id"
        type="button"
        class="link-button board-hidden-show"
        @click="showProject(h.id)"
      >
        показать {{ h.name }}
      </button>
    </p>
    <button
      type="button"
      class="link-button board-traffic"
      @click="store.openClaude()"
    >
      Переписка агентов
    </button>
  </aside>
</template>
