<script setup lang="ts">
// Ф41-2б-2: блок «Сессии проекта» на вкладке «Claude» (и на телефоне — тот же): список постоянных сессий хаба
// проекта, строка раскрывается в окно, «Новая сессия». Блок держит опрос списка (раз в 30 с — ради `cache` и
// `listening`, их нет в `/sync`), пока смонтирован. Названия, роли и причины — чужой ввод: только текстом.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import { store } from '../store/instance';
import { sessionEngineLabel } from '../util/engineProfiles';
import { sessionStateText } from '../util/sessionBg';
import SessionBgChip from './SessionBgChip.vue';
import { INTERRUPTED_STATE, SESSION_STATE_LABEL, SESSION_WAIT_LABEL, hasInterrupted, sessionName, sessionRoleLabel } from '../util/sessions';
import NewSessionForm from './NewSessionForm.vue';
import SessionCacheLine from './SessionCacheLine.vue';
import SessionWindow from './SessionWindow.vue';

const props = defineProps<{ projectId: number; nowMs?: number }>();

const showNew = ref(false);
let stopWatch: (() => void) | null = null;

onMounted(() => {
  stopWatch = store.watchSessions();
});
onBeforeUnmount(() => stopWatch?.());

const sessions = computed(() =>
  Object.values(store.state.sessions.items)
    .filter((s) => s.project_id === props.projectId && s.closed_ts === null)
    .sort((a, b) => a.id - b.id),
);

function isOpen(id: number): boolean {
  return store.state.sessions.expanded[id] === true;
}
</script>

<template>
  <div
    class="board-section session-block"
    data-section="sessions"
  >
    <div class="session-block-head">
      <h3 class="board-section-title">
        Сессии проекта
      </h3>
      <button
        type="button"
        class="link-button session-new-toggle"
        :aria-expanded="showNew"
        @click="showNew = !showNew"
      >
        Новая сессия
      </button>
    </div>
    <NewSessionForm
      v-if="showNew"
      :project-id="projectId"
      @done="showNew = false"
      @cancel="showNew = false"
    />
    <p
      v-if="store.state.sessions.error"
      class="home-error"
      role="alert"
    >
      {{ store.state.sessions.error }}
    </p>
    <p
      v-if="sessions.length === 0"
      class="board-empty"
    >
      Сессий пока нет
    </p>
    <ul
      v-else
      class="session-list"
    >
      <li
        v-for="session in sessions"
        :key="session.id"
        class="session-row"
        :data-session-id="session.id"
      >
        <button
          type="button"
          class="session-row-head"
          :aria-expanded="isOpen(session.id)"
          @click="store.toggleSessionWindow(session.id)"
        >
          <span class="session-name">{{ sessionName(session) }}</span>
          <span
            v-if="session.title && session.role"
            class="session-role"
          >{{ sessionRoleLabel(session) }}</span>
          <span class="session-engine">{{ sessionEngineLabel(session.engine) }}</span>
          <span
            class="session-state"
            :class="`session-state--${session.state}`"
          >{{ sessionStateText(session, SESSION_STATE_LABEL) }}</span>
          <span
            v-if="session.pending_requests > 0"
            class="session-pending"
          >ждёт тебя: {{ session.pending_requests }}</span>
          <span
            v-if="hasInterrupted(session)"
            class="session-pending session-interrupted-state"
          >{{ INTERRUPTED_STATE }}</span>
        </button>
        <SessionBgChip
          v-if="!isOpen(session.id)"
          :session="session"
        />
        <p
          v-if="session.wait_reason"
          class="session-wait"
        >
          {{ SESSION_WAIT_LABEL[session.wait_reason] }}
        </p>
        <p
          v-if="session.failure"
          class="work-task-failure session-failure"
        >
          {{ session.failure }}
        </p>
        <SessionCacheLine
          v-if="session.cache || session.usage"
          :cache="session.cache"
          :usage="session.usage"
          :engine="session.engine"
          :compact="session"
          :now-ms="nowMs"
        />
        <SessionWindow
          v-if="isOpen(session.id)"
          :session="session"
        />
      </li>
    </ul>
  </div>
</template>
