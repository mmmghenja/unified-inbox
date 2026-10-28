<script setup lang="ts">
// Ф20-4 C (`docs/architecture.md`): столбец доски — один проект (или «Неразобранное»), «тонкий» вариант A.
// Сверху вниз: липкая шапка (цвет, имя — кнопка на вкладку проекта, строка состояния), «Нужна реакция» (строки бесед
// + то, что ждёт решения владельца: карточки сессий хаба, «Вступить?», «Выйти?»), «Черновики»
// строками `DraftRow` (первые 8, дальше «ещё N ▾») над ними пачка `DraftBatchBar`, «По графику», внизу одна серая
// строка про Claude со ссылкой на вкладку «Claude» и свёрнутое «Отправлено сегодня». Пустые разделы не показываются;
// пусто всё — «Ничего не ждёт». Агенты и их переписка — «⋯ → Агенты» (`ClaudeView`); задач по «Го» больше нет (Ф78-1).
//
// Всё из переписки и от моделей (названия, имена, превью, тексты черновиков) — недоверенный ввод: только текстовая
// интерполяция Vue.
import { computed, ref } from 'vue';

import { type HomeReactItem, type OutboxListItem, UNSORTED_PROJECT_ID } from '../api/types';
import { store } from '../store/instance';
import { type BoardColumn, type UnsortedColumn } from '../util/board';
import { safeColor } from '../util/color';
import { filterByNumber } from '../util/draftNumber';
import { formatTime, sentVia } from '../util/home';
import { showMentions } from '../util/mentions';
import { sourceLabel } from '../util/people';
import { STALE_RETRY_TEXT } from '../util/dispatch';
import { interruptedWaitLabel, sessionName, sessionRequestLabel } from '../util/sessions';
import { heavyWaitLabel } from '../util/sessionSub';
import { buildWaiting } from '../util/waiting';
import DayControl from './DayControl.vue';
import DraftBatchBar from './DraftBatchBar.vue';
import DraftRow from './DraftRow.vue';
import NumberSearch from './NumberSearch.vue';
import JoinCard from './JoinCard.vue';
import HumanCard from './HumanCard.vue';
import PressCard from './PressCard.vue';
import GroupOpCard from './GroupOpCard.vue';
import LeaveCard from './LeaveCard.vue';
import ReminderCard from './ReminderCard.vue';
import AgentRequestCard from './AgentRequestCard.vue';
import RuleRequestCard from './RuleRequestCard.vue';
import WorkRequestCard from './WorkRequestCard.vue';

/** Сколько строк черновиков видно в столбце, остальные — по «ещё N ▾». */
const DRAFT_ROWS = 8;

const props = defineProps<{ column: BoardColumn | UnsortedColumn }>();

const project = computed(() => (props.column.kind === 'project' ? props.column : null));
const projectId = computed(() => project.value?.project.id ?? UNSORTED_PROJECT_ID);
const workdir = computed(() => project.value?.project.workdir ?? null);
const runs = computed(() => project.value?.runs ?? []);
// Ф11-4 И4: у «Неразобранного» — своё отправленное сегодня, тем же разделом, что у проектов.
const sent = computed(() => props.column.sent);

const sentOpen = ref(false);
const draftsExpanded = ref(false);
const expandedSent = ref<Record<number, boolean>>({});

function openTab(): void {
  void store.openProject(projectId.value);
}

// --- «Нужна реакция» ---------------------------------------------------------------------------------------

function ruleTitle(item: HomeReactItem, id: number): string {
  const title = item.rules?.find((r) => r.id === id)?.title;
  return title ? `Сработало правило входящих: ${title}` : 'Сработало правило входящих проекта';
}

function reactCount(item: HomeReactItem): number {
  return item.channel.unread_react ?? item.channel.unread;
}

function lastLabel(item: HomeReactItem): string {
  return `${item.last.sender ?? '(отправитель неизвестен)'}: ${item.last.excerpt ?? '(без текста)'}`;
}

function openReact(item: HomeReactItem): void {
  const id = item.project.id > 0 ? item.project.id : null;
  void store.openChannelAt(id, item.channel.id, {
    messageId: item.last.message_id,
    threadExtId: item.last.thread_ext_id ?? null,
  });
}

// Карточки сессий хаба проекта — те же, что во «Ждёт» телефона (`util/waiting.ts`);
// заявки на вступление и выход столбец уже получил видимыми (`column.joins|leaves`).
const waiting = computed(() => {
  if (project.value === null) return [];
  return buildWaiting({
    projects: store.state.projects,
    home: { react: [], drafts: [] },
    sessions: store.state.sessions,
  }).filter((item) => item.project.id === projectId.value);
});
const sessionRequests = computed(() => waiting.value.flatMap((item) => (item.kind === 'session_request' ? [item] : [])));
const sessionHeavy = computed(() => waiting.value.flatMap((item) => (item.kind === 'session_heavy' ? [item] : [])));
const sessionInterrupted = computed(() => waiting.value.flatMap((item) => (item.kind === 'session_interrupted' ? [item] : [])));
const joins = computed(() => project.value?.joins ?? []);
const leaves = computed(() => project.value?.leaves ?? []);
const groupOps = computed(() => project.value?.groupOps ?? []);
const ruleRequests = computed(() => project.value?.ruleRequests ?? []);
const agentRequests = computed(() => project.value?.agentRequests ?? []);
const presses = computed(() => props.column.kind === 'project' ? props.column.presses : []);
const humans = computed(() => props.column.kind === 'project' ? props.column.humans : []);
const reminders = computed(() => props.column.reminders);
const reactTotal = computed(
  () =>
    props.column.react.length + sessionRequests.value.length + sessionHeavy.value.length + sessionInterrupted.value.length + joins.value.filter((j) => j.status === 'pending').length + ruleRequests.value.filter((r) => r.status === 'pending').length + agentRequests.value.length + presses.value.length + humans.value.length + leaves.value.length + groupOps.value.length + reminders.value.length,
);

// Протухшая карточка сессии: «разрешено, повтори» — репликой в очередь этой сессии.
async function retrySession(sessionId: number): Promise<'ok' | 'error'> {
  const result = await store.saySession(sessionId, STALE_RETRY_TEXT, 'next');
  return result.ok ? 'ok' : 'error';
}

// --- «Черновики» --------------------------------------------------------------------------------------------

const numberText = ref('');
const drafts = computed(() => filterByNumber(props.column.drafts, numberText.value));
const scheduledItems = computed(() => filterByNumber(props.column.scheduled, numberText.value));
const shownDrafts = computed<OutboxListItem[]>(() =>
  draftsExpanded.value ? drafts.value : drafts.value.slice(0, DRAFT_ROWS),
);
const hiddenDrafts = computed(() => Math.max(0, drafts.value.length - DRAFT_ROWS));
const nothingWaits = computed(
  () => reactTotal.value === 0 && drafts.value.length === 0 && props.column.scheduled.length === 0,
);

// --- «Claude сегодня» (серая строка) -----------------------------------------------------------------------

const sessionState = computed(() => {
  const col = project.value;
  if (col === null) return '';
  if (col.state.wait) return 'ждёт';
  return 'свободна';
});

function openClaude(): void {
  store.revealProject(projectId.value);
}

// --- «Отправлено сегодня» -------------------------------------------------------------------------------------

function toggleSent(item: OutboxListItem): void {
  expandedSent.value[item.id] = !expandedSent.value[item.id];
}
</script>

<template>
  <section
    class="board-column"
    :class="{ 'board-column--unsorted': project === null }"
    :data-project-id="projectId"
    :aria-label="project ? (project.project.name ?? '(без названия)') : 'Неразобранное'"
  >
    <header class="board-column-head">
      <span
        v-if="project"
        class="color-dot"
        :style="{ backgroundColor: safeColor(project.project.color) }"
      />
      <button
        type="button"
        class="board-column-name"
        :title="project ? 'Открыть вкладку проекта' : 'Открыть «Неразобранное»'"
        @click="openTab"
      >
        {{ project ? (project.project.name ?? '(без названия)') : 'Неразобранное' }}
      </button>
      <span
        v-if="project && project.state.line"
        class="board-state"
        :class="{ 'board-state--wait': project.state.wait }"
      >{{ project.state.line }}</span>
      <DayControl
        v-if="project"
        :project-id="projectId"
      />
    </header>

    <p
      v-if="nothingWaits"
      class="board-empty board-nothing"
    >
      Ничего не ждёт
    </p>

    <div
      v-if="reactTotal > 0"
      class="board-section board-react"
      data-section="react"
    >
      <h3 class="board-section-title">
        Нужна реакция · {{ reactTotal }}
      </h3>
      <ul v-if="column.react.length > 0">
        <li
          v-for="item in column.react"
          :key="item.channel.id"
        >
          <button
            type="button"
            class="home-react-row"
            @click="openReact(item)"
          >
            <span class="home-react-line">
              <span class="home-react-address">{{ item.channel.title ?? '(без названия)' }}</span>
              <span class="home-react-source">· {{ sourceLabel(item.channel.source ?? '') }}</span>
              <span class="home-react-time">{{ formatTime(item.last.ts) }}</span>
              <span class="badge badge-react">{{ reactCount(item) }}</span>
              <span
                v-if="item.draft_count > 0"
                class="home-react-draft"
              >черновик</span>
              <span
                v-for="id in item.rule_hits ?? []"
                :key="`rule-${id}`"
                class="rule-mark"
                :title="ruleTitle(item, id)"
              >правило #{{ id }}</span>
            </span>
            <span class="home-react-last">{{ lastLabel(item) }}</span>
          </button>
        </li>
      </ul>
      <div
        v-for="item in sessionRequests"
        :key="item.key"
        class="board-wait-card"
        data-section="session-requests"
      >
        <p class="wait-task-title wait-session-title">
          {{ sessionRequestLabel(item.session, item.request) }}
        </p>
        <WorkRequestCard
          v-if="item.request !== null"
          :request="item.request"
          :stale="item.stale"
          one-line
          :decide="(body) => store.decideSessionRequest(item.request!, body)"
          :retry="() => retrySession(item.session.id)"
          :dismiss="() => store.dismissStaleSessionRequest(item.request!.id)"
        />
        <button
          type="button"
          class="link-button wait-session-open"
          @click="store.revealSession(item.session.project_id, item.session.id)"
        >
          Открыть сессию
        </button>
      </div>
      <div
        v-for="item in sessionHeavy"
        :key="item.key"
        class="board-wait-card"
        data-section="session-heavy"
      >
        <p class="wait-task-title wait-session-title">
          {{ heavyWaitLabel(sessionName(item.session), item.count) }}
        </p>
        <button
          type="button"
          class="wait-session-wake"
          @click="store.wakeSession(item.session.id)"
        >
          Поднять
        </button>
        <button
          type="button"
          class="link-button wait-session-open"
          @click="store.revealSession(item.session.project_id, item.session.id)"
        >
          Открыть сессию
        </button>
      </div>
      <div
        v-for="item in sessionInterrupted"
        :key="item.key"
        class="board-wait-card"
        data-section="session-interrupted"
      >
        <p class="wait-task-title wait-session-title">
          {{ interruptedWaitLabel(item.session) }}
        </p>
        <button
          type="button"
          class="link-button wait-session-open"
          @click="store.revealSession(item.session.project_id, item.session.id)"
        >
          Открыть сессию
        </button>
      </div>
      <div
        v-if="reminders.length > 0"
        class="board-wait-card"
        data-section="reminders"
      >
        <ReminderCard
          v-for="reminder in reminders"
          :key="reminder.id"
          :reminder="reminder"
        />
      </div>
      <div
        v-if="joins.length > 0"
        class="board-wait-card"
        data-section="joins"
      >
        <JoinCard
          v-for="join in joins"
          :key="join.id"
          :join="join"
        />
      </div>
      <div
        v-if="ruleRequests.length > 0"
        class="board-wait-card"
        data-section="rule-requests"
      >
        <RuleRequestCard
          v-for="request in ruleRequests"
          :key="request.id"
          :request="request"
        />
      </div>
      <div
        v-if="humans.length > 0"
        class="board-wait-card"
        data-section="humans"
      >
        <HumanCard
          v-for="human in humans"
          :key="human.id"
          :human="human"
        />
      </div>
      <div
        v-if="presses.length > 0"
        class="board-wait-card"
        data-section="presses"
      >
        <PressCard
          v-for="press in presses"
          :key="press.id"
          :press="press"
        />
      </div>
      <div
        v-if="agentRequests.length > 0"
        class="board-wait-card"
        data-section="agent-requests"
      >
        <AgentRequestCard
          v-for="request in agentRequests"
          :key="request.id"
          :request="request"
        />
      </div>
      <div
        v-if="leaves.length > 0"
        class="board-wait-card"
        data-section="leaves"
      >
        <LeaveCard
          v-for="leave in leaves"
          :key="leave.id"
          :leave="leave"
        />
      </div>
      <div
        v-if="groupOps.length > 0"
        class="board-wait-card"
        data-section="group-ops"
      >
        <GroupOpCard
          v-for="op in groupOps"
          :key="op.id"
          :op="op"
        />
      </div>
    </div>

    <NumberSearch
      v-if="column.drafts.length + column.scheduled.length > 1"
      v-model="numberText"
      class="board-number-search"
    />

    <div
      v-if="column.drafts.length > 0"
      class="board-section board-drafts"
      data-section="drafts"
    >
      <h3 class="board-section-title">
        Черновики · {{ drafts.length }}
      </h3>
      <DraftBatchBar :items="drafts" />
      <DraftRow
        v-for="item in shownDrafts"
        :key="item.id"
        :item="item"
        selectable
      />
      <button
        v-if="hiddenDrafts > 0"
        type="button"
        class="link-button draft-more"
        :aria-expanded="draftsExpanded"
        @click="draftsExpanded = !draftsExpanded"
      >
        {{ draftsExpanded ? 'свернуть ▴' : `ещё ${hiddenDrafts} ▾` }}
      </button>
    </div>

    <!-- Ф12-2 C: подтверждённые по графику — когда уйдут, «Отменить». Ф30-1 П3: и к точному времени
         («Запланировано», если есть такие). -->
    <div
      v-if="column.scheduled.length > 0"
      class="board-section board-scheduled"
      data-section="scheduled"
    >
      <h3 class="board-section-title board-scheduled-title">
        {{ column.scheduled.some((item) => typeof item.send_at_ts === 'number') ? 'Запланировано' : 'По графику' }} ·
        {{ column.scheduled.length }}
      </h3>
      <DraftRow
        v-for="item in scheduledItems"
        :key="item.id"
        :item="item"
      />
    </div>

    <p
      v-if="project && workdir"
      class="board-claude-line"
      data-section="claude"
    >
      Claude сегодня: {{ runs.length }} · сессия: {{ sessionState }} ·
      <button
        type="button"
        class="link-button board-claude-link"
        @click="openClaude"
      >
        Агенты ›
      </button>
    </p>

    <div
      v-if="sent.length > 0"
      class="board-section board-sent"
      data-section="sent"
    >
      <template v-if="sent.length > 0">
        <button
          type="button"
          class="board-section-toggle"
          :aria-expanded="sentOpen"
          @click="sentOpen = !sentOpen"
        >
          Отправлено сегодня · {{ sent.length }}
        </button>
        <ul v-if="sentOpen">
          <li
            v-for="item in sent"
            :key="item.id"
            class="home-sent-row"
          >
            <span class="home-sent-time">{{ formatTime(item.sent_ts ?? item.created_ts) }}</span>
            <span class="home-sent-address">{{ item.channel_title ?? '(без названия)' }}</span>
            <span class="draft-card-num draft-card-num--quiet">#{{ item.id }}</span>
            <span class="home-sent-via">{{ sentVia(item) }}</span>
            <!-- Ф37-1 C2: без подтверждения — роль сессии -->
            <span
              v-if="item.auto_role || item.auto_session != null"
              class="home-sent-auto"
            >без подтверждения{{ item.auto_role ? ` · ${item.auto_role}` : '' }}{{ item.auto_session != null ? ' · автопилот' : '' }}</span>
            <button
              type="button"
              class="home-sent-text"
              :class="{ 'home-sent-text--open': expandedSent[item.id] }"
              :aria-expanded="expandedSent[item.id] === true"
              @click="toggleSent(item)"
            >
              {{ showMentions(item.text ?? '', item.mention_names) }}
            </button>
          </li>
        </ul>
      </template>
    </div>
  </section>
</template>
