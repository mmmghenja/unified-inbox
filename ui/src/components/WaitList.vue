<script setup lang="ts">
// Ф10-6 B4: «Ждёт» телефона — всё, что ждёт владельца, со всех проектов (`util/waiting.ts`), у каждой карточки метка
// «цвет · проект». Порядок: карточки сессий хаба -> заявки -> черновики -> беседы «нужна реакция». Загрузка при показе и раз в 30 с, пока вкладка открыта. Тексты из переписки и от
// моделей — только интерполяцией.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import type { HomeReactItem } from '../api/types';
import { store } from '../store/instance';
import { HOME_POLL_MS } from '../store/store';
import { safeColor } from '../util/color';
import { STALE_RETRY_TEXT } from '../util/dispatch';
import { filterByNumber, numberQuery } from '../util/draftNumber';
import { formatTime } from '../util/home';
import { interruptedWaitLabel, sessionName, sessionRequestLabel } from '../util/sessions';
import { heavyWaitLabel } from '../util/sessionSub';
import { buildWaiting } from '../util/waiting';
import { waitingInputOf } from '../util/waitInput';
import ClosedDayLines from './ClosedDayLines.vue';
import DraftBatchBar from './DraftBatchBar.vue';
import DraftRow from './DraftRow.vue';
import JoinCard from './JoinCard.vue';
import NumberSearch from './NumberSearch.vue';
import GroupOpCard from './GroupOpCard.vue';
import LeaveCard from './LeaveCard.vue';
import ReminderCard from './ReminderCard.vue';
import AgentRequestCard from './AgentRequestCard.vue';
import HumanCard from './HumanCard.vue';
import PressCard from './PressCard.vue';
import RuleRequestCard from './RuleRequestCard.vue';
import WorkRequestCard from './WorkRequestCard.vue';

const items = computed(() =>
  buildWaiting(waitingInputOf(store)),
);
// Ф12-2 C: черновики «Ждёт» — для «Выбрать все» / «Подтвердить выбранные» в порядке списка.
const numberText = ref('');
const allDrafts = computed(() => items.value.flatMap((item) => (item.kind === 'draft' ? [item.draft] : [])));
// Поле «№»: черновики, чей номер не подходит, скрыты; прочие карточки остаются.
const visibleItems = computed(() => {
  if (numberQuery(numberText.value) === '') return items.value;
  const keep = new Set(filterByNumber(allDrafts.value, numberText.value).map((d) => d.id));
  return items.value.filter((item) => item.kind !== 'draft' || keep.has(item.draft.id));
});
const drafts = computed(() => filterByNumber(allDrafts.value, numberText.value));

let pollTimer: ReturnType<typeof setInterval> | null = null;

function load(): void {
  void store.loadHome();
  void store.loadSessions();
  void store.loadJoins();
  void store.loadRuleRequests();
  void store.loadAgentRequests();
  void store.loadPresses();
  void store.loadHumanRequests();
  void store.loadLeaves();
  void store.loadGroupOps();
}

onMounted(() => {
  load();
  pollTimer = setInterval(load, HOME_POLL_MS);
});

onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer);
});

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
  const projectId = item.project.id > 0 ? item.project.id : null;
  void store.openChannelAt(projectId, item.channel.id, {
    messageId: item.last.message_id,
    threadExtId: item.last.thread_ext_id ?? null,
  });
}

// Протухшая карточка сессии: «разрешено, повтори» — репликой в очередь этой сессии.
async function retrySession(sessionId: number): Promise<'ok' | 'error'> {
  const result = await store.saySession(sessionId, STALE_RETRY_TEXT, 'next');
  return result.ok ? 'ok' : 'error';
}
</script>

<template>
  <section
    class="wait-list"
    aria-label="Ждёт"
  >
    <p
      v-if="store.state.home.error"
      class="home-error"
      role="alert"
    >
      {{ store.state.home.error }}
    </p>
    <p
      v-if="items.length === 0"
      class="empty-hint wait-empty"
    >
      Ничего не ждёт.
    </p>
    <ClosedDayLines />
    <NumberSearch
      v-if="allDrafts.length > 1"
      v-model="numberText"
    />
    <DraftBatchBar :items="drafts" />
    <article
      v-for="item in visibleItems"
      :key="item.key"
      class="wait-item"
      :class="`wait-item--${item.kind}`"
    >
      <p class="wait-project">
        <span
          class="color-dot"
          :style="{ backgroundColor: safeColor(item.project.color) }"
        />
        <span class="wait-project-name">{{ item.project.name }}</span>
      </p>

      <template v-if="item.kind === 'session_request'">
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
      </template>

      <template v-else-if="item.kind === 'session_interrupted'">
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
      </template>

      <template v-else-if="item.kind === 'session_heavy'">
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
          class="wait-session-handoff"
          @click="store.handoffSession(item.session.id, { engine: item.session.engine, model: item.session.model, effort: item.session.effort, mode: item.session.mode })"
        >
          Новая с передачей дел
        </button>
        <button
          type="button"
          class="link-button wait-session-open"
          @click="store.revealSession(item.session.project_id, item.session.id)"
        >
          Открыть сессию
        </button>
      </template>

      <JoinCard
        v-else-if="item.kind === 'join'"
        :join="item.join"
      />

      <RuleRequestCard
        v-else-if="item.kind === 'rule'"
        :request="item.request"
      />

      <AgentRequestCard
        v-else-if="item.kind === 'agent_request'"
        :request="item.request"
      />

      <HumanCard
        v-else-if="item.kind === 'human'"
        :human="item.human"
      />

      <PressCard
        v-else-if="item.kind === 'press'"
        :press="item.press"
      />

      <LeaveCard
        v-else-if="item.kind === 'leave'"
        :leave="item.leave"
      />

      <GroupOpCard
        v-else-if="item.kind === 'group_op'"
        :op="item.op"
      />

      <ReminderCard
        v-else-if="item.kind === 'reminder'"
        :reminder="item.reminder"
      />

      <DraftRow
        v-else-if="item.kind === 'draft'"
        :item="item.draft"
        selectable
        show-project
      />

      <button
        v-else-if="item.kind === 'react'"
        type="button"
        class="wait-row wait-row--react"
        @click="openReact(item.item)"
      >
        <span class="wait-row-head">
          <span class="wait-row-title">{{ item.project.name }} · {{ item.item.channel.title ?? '(без названия)' }}</span>
          <span class="badge badge-react">{{ reactCount(item.item) }}</span>
          <span class="wait-row-time">{{ formatTime(item.item.last.ts) }}</span>
          <span
            v-for="id in item.item.rule_hits ?? []"
            :key="`rule-${id}`"
            class="rule-mark"
            :title="ruleTitle(item.item, id)"
          >правило #{{ id }}</span>
        </span>
        <span class="wait-row-last">{{ lastLabel(item.item) }}</span>
      </button>
    </article>
  </section>
</template>
