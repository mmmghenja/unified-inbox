<script setup lang="ts">
// Ф41-6б (раздел 3): вкладка «Сессии» телефона. Шапка «Сессии» и «Новая»; строка «Ждёт тебя · N» (переход на вкладку
// «Ждёт»); по проектам — шапка с красным счётчиком и строки сессий (имя, подпись «модель · ручной/
// авто · состояние · кэш»); внешние — «как агенту». Тап — на весь экран: окно сессии или диалог агента; «Назад» — общим механизмом уровней телефона (`goBack('session')`). Внизу — «Переписка агентов»
// (страница «Агенты»). Раскладка доски на телефоне не используется. Названия — чужой ввод: текстом.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import { loadLayout, loadOfflineLines, boardState, hideOfflineLine, type ColumnItem } from '../store/board';
import { HOME_POLL_MS } from '../store/store';
import { store } from '../store/instance';
import { openWrite } from '../store/writeModal';
import { buildAgentLines, unreadRepliesCount } from '../store/peerMsgs';
import { buildBoardList, phoneSessionSub, waitSummary } from '../util/boardList';
import { safeColor } from '../util/color';
import { sessionName } from '../util/sessions';
import { buildWaiting } from '../util/waiting';
import { waitingInputOf } from '../util/waitInput';
import AgentDialog from './AgentDialog.vue';
import AgentUnsubscribe from './AgentUnsubscribe.vue';
import ClosedDayLines from './ClosedDayLines.vue';
import MachineChip from './MachineChip.vue';
import FormFrame from './FormFrame.vue';
import NewSessionForm from './NewSessionForm.vue';
import type { SessionWindowHandle } from '../util/sessionHandle';
import SessionHeadBar from './SessionHeadBar.vue';
import SessionWindow from './SessionWindow.vue';

const nowTick = ref(Date.now());
let pollTimer: ReturnType<typeof setInterval> | null = null;
let tickTimer: ReturnType<typeof setInterval> | null = null;
let stopSessions: (() => void) | null = null;
let stopAgents: (() => void) | null = null;
const showNew = ref(false);
const win = ref<SessionWindowHandle | null>(null);

function load(): void {
  void store.loadHome();
  void store.loadJoins();
  void store.loadRuleRequests();
  void store.loadAgentRequests();
  void store.loadPresses();
  void store.loadHumanRequests();
  void store.loadLeaves();
  void store.loadGroupOps();
  void store.loadPeerMsgs();
  void loadOfflineLines();
}

onMounted(() => {
  load();
  // Убранные «×» линии «не в сети» лежат в раскладке доски (ядро, общая с ноутбуком); главная на телефоне её не
  // читает — без этого телефон показывал все давно убранные линии снова.
  if (!boardState.loaded) void loadLayout();
  stopSessions = store.watchSessions();
  stopAgents = store.watchAgentSubs();
  void store.refreshAgentSubs(); // сразу, а не через полминуты: новая сессия видна, как только открыл
  pollTimer = setInterval(load, HOME_POLL_MS);
  tickTimer = setInterval(() => {
    nowTick.value = Date.now();
  }, 30_000);
});

onBeforeUnmount(() => {
  stopSessions?.();
  stopAgents?.();
  if (pollTimer !== null) clearInterval(pollTimer);
  if (tickTimer !== null) clearInterval(tickTimer);
});

const waiting = computed(() =>
  buildWaiting(waitingInputOf(store)),
);
const summary = computed(() => waitSummary(waiting.value, [], store.state.projects));
const lines = computed(() => buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs));
const groups = computed(() =>
  buildBoardList({
    projects: store.state.projects,
    sessions: Object.values(store.state.sessions.items),
    pending: store.state.sessions.pending,
    agentLines: lines.value,
    offline: boardState.offline,
    hiddenLines: boardState.layout.hidden_lines,
    home: store.state.home,
    waiting: waiting.value,
    hiddenProjects: [],
    nowMs: nowTick.value,
  }),
);
// Непрочитанные ответы агентов владельцу: то же число, что значок у вкладки «Сессии», — строкой с именем агента.
const replyLines = computed(() =>
  lines.value
    .filter((l) => l.unread > 0)
    .map((l) => ({
      key: l.key,
      count: l.unread,
      name: l.name,
      project: store.state.projects.find((p) => p.id === l.projectId)?.name ?? null,
    })),
);
const repliesElsewhere = computed(
  () => unreadRepliesCount(store.state.peerMsgs.items) - replyLines.value.reduce((n, l) => n + l.count, 0),
);
const waitLine = computed(() => summary.value.byProject.map((e) => `${e.name} ${e.count}`).join(', '));

const current = computed<ColumnItem | null>(() => store.state.phoneSession);
const currentSession = computed(() => {
  const item = current.value;
  return item?.kind === 'session' && item.id !== undefined ? (store.state.sessions.items[item.id] ?? null) : null;
});
const currentLine = computed(() => {
  const item = current.value;
  return item?.kind === 'agent' ? (lines.value.find((l) => l.key === item.key) ?? null) : null;
});
const currentTitle = computed(() => {
  const item = current.value;
  if (item === null) return '';
  if (item.kind === 'session') return currentSession.value === null ? 'сессия' : sessionName(currentSession.value);
  return currentLine.value?.name ?? 'сессия';
});

// Проект сессии рядом с ролью в шапке окна (владелец 15.10: «проекта не видно»).
const currentProject = computed(() => {
  const item = current.value;
  const projectId = item?.kind === 'session' ? currentSession.value?.project_id : currentLine.value?.projectId;
  if (projectId === null || projectId === undefined) return null;
  return store.state.projects.find((p) => p.id === projectId)?.name ?? null;
});

async function removeOffline(key: string): Promise<void> {
  // Раскладка ещё не прочитана — сначала прочитать: иначе «×» сохранил бы пустую раскладку поверх ноутбучной.
  if (!boardState.loaded) await loadLayout();
  const line = boardState.offline.find((l) => l.key === key);
  if (line !== undefined) hideOfflineLine(line);
}

function open(item: ColumnItem): void {
  store.openPhoneSession(item);
}

function back(): void {
  store.goBack('session');
}
</script>

<template>
  <section
    v-if="current !== null"
    class="phone-session-full"
    :aria-label="currentTitle"
  >
    <SessionHeadBar
      v-if="current.kind === 'session' && currentSession !== null"
      :session="currentSession"
      :win="win"
      back
      @back="back"
    />
    <header
      v-else
      class="phone-session-head"
    >
      <button
        type="button"
        class="back-button"
        aria-label="Назад к сессиям"
        @click="back"
      >
        ‹
      </button>
      <b class="phone-session-title">{{ currentTitle }}</b>
      <span
        v-if="currentProject !== null"
        class="phone-session-project"
      >· {{ currentProject }}</span>
      <MachineChip :machine="currentLine?.sub?.machine" />
    </header>
    <div class="phone-session-body">
      <SessionWindow
        v-if="current.kind === 'session' && currentSession !== null"
        ref="win"
        :session="currentSession"
        column
      />
      <template v-else-if="current.kind === 'agent' && currentLine !== null">
        <AgentUnsubscribe
          v-if="currentLine.sub !== null"
          :sub-id="currentLine.sub.id"
          @done="back"
        />
        <AgentDialog :line="currentLine" />
      </template>
      <p
        v-else
        class="board-empty"
      >
        Сессия закрыта или ещё не загрузилась.
      </p>
    </div>
  </section>
  <main
    v-else
    class="phone-sessions"
    aria-label="Сессии"
  >
    <header class="phone-sessions-head">
      <button
        type="button"
        class="phone-sessions-new phone-sessions-write"
        data-test="phone-write"
        @click="openWrite()"
      >
        Написать…
      </button>
      <button
        type="button"
        class="phone-sessions-new"
        :aria-expanded="showNew"
        @click="showNew = !showNew"
      >
        Новая
      </button>
    </header>
    <FormFrame
      v-if="showNew"
      :modal="false"
      title="Новая сессия"
      @close="showNew = false"
    >
      <NewSessionForm
        @done="showNew = false"
        @cancel="showNew = false"
      />
    </FormFrame>
    <button
      type="button"
      class="phone-sessions-wait"
      @click="store.setPhoneTab('wait')"
    >
      <template v-if="summary.total > 0">
        Ждёт тебя · {{ summary.total }}<span v-if="waitLine"> — {{ waitLine }}</span>
      </template>
      <template v-else>
        Ничего не ждёт
      </template>
    </button>
    <ClosedDayLines />
    <button
      v-for="reply in replyLines"
      :key="`r${reply.key}`"
      type="button"
      class="phone-sessions-reply"
      :data-reply-key="reply.key"
      @click="open({ kind: 'agent', key: reply.key })"
    >
      <span class="badge badge-react">{{ reply.count }}</span>
      Ответ тебе: {{ reply.name }}<template v-if="reply.project">
        · {{ reply.project }}
      </template>
    </button>
    <button
      v-if="repliesElsewhere > 0"
      type="button"
      class="phone-sessions-reply"
      data-reply-key="other"
      @click="store.openClaude()"
    >
      <span class="badge badge-react">{{ repliesElsewhere }}</span>
      Ответы агентов — в переписке
    </button>
    <section
      v-for="group in groups"
      :key="group.id"
      class="board-group"
      :data-project-id="group.id"
    >
      <header class="board-group-head">
        <span class="board-group-name">
          <span
            class="color-dot"
            :style="{ backgroundColor: safeColor(group.color) }"
          />
          {{ group.name }}
        </span>
        <span
          v-if="group.waitCount > 0"
          class="badge badge-react"
        >{{ group.waitCount }}</span>
      </header>
      <button
        v-for="row in group.sessions"
        :key="`s${row.id}`"
        type="button"
        class="board-row board-row--session"
        :data-session-id="row.id"
        @click="open(row.item)"
      >
        <span
          class="board-dot"
          :class="`board-dot--${row.dot}`"
        />
        <span class="board-row-name">{{ row.name }}</span>
        <MachineChip :machine="row.machine" />
        <span class="board-row-sub phone-row-sub">{{ phoneSessionSub(row) }}</span>
      </button>
      <div
        v-for="row in group.agents"
        :key="`a${row.key}`"
        :class="{ 'board-row-wrap': row.offline }"
      >
        <button
          type="button"
          class="board-row board-row--agent"
          :data-agent-key="row.key"
          @click="open(row.item)"
        >
          <span
            class="board-dot"
            :class="`board-dot--${row.dot}`"
          />
          <span class="board-row-name">{{ row.name }}</span>
          <MachineChip :machine="row.machine" />
          <span class="board-row-sub">{{ row.sub }} · как агенту</span>
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
    </section>
    <footer class="phone-sessions-foot">
      <button
        type="button"
        class="phone-sessions-traffic"
        @click="store.openClaude()"
      >
        Переписка агентов
      </button>
    </footer>
  </main>
</template>
