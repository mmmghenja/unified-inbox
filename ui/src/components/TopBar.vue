<script setup lang="ts">
// Верхняя полоса (Ф10-6 B2, `docs/architecture.md`; режимы `full`/`half`). Одна строка: слева
// вкладки (решение 3) — «Главная» (красный счётчик решений: запросы сессий + предложения + черновики), проекты в
// порядке API (цвет, имя, красный «реагирую»/серый прочее), «Неразобранное», «Все» (без счётчика, последней).
// Полоса вкладок не переносится — лишнее прокручивается вбок, активная вкладка прокручивается в видимую часть.
// Ф20-4 A: счётчик вкладки — только красный («реагирую»); серого «к сведению» в полосе нет. После вкладок проектов —
// «Claude» (оркестратор и сессии по проектам, без счётчика). Справа — одно меню «⋯» (`TopMenu.vue`): «Новое
// сообщение», поиск, раскладка, доступ, движок, строка состояния; тревоги (аккаунты, связь, сбои очереди) — в
// полосе сами.
// Ф90-2 (Р3): на «Главной» в этой же полосе — кнопка-счётчик «Ждёт тебя · N»: по клику панель поверх доски (`WaitBar`).
// Ф8-3 D4: браузерных уведомлений нет — их даёт разборщик.
import { computed, nextTick, ref, watch } from 'vue';

import { boardState, toggleWait } from '../store/board';
import { store } from '../store/instance';
import { unreadRepliesCount } from '../store/peerMsgs';
import { termState, toggleTerminal } from '../store/terminal';
import { openWrite } from '../store/writeModal';
import { safeColor } from '../util/color';
import { waitSummary } from '../util/boardList';
import { projectTabs, type ProjectTab } from '../util/tabs';
import { buildWaiting, homeDecisionCount } from '../util/waiting';
import { waitingInputOf } from '../util/waitInput';
import TopMenu from './TopMenu.vue';

const tabs = computed(() => projectTabs(store.state.projects));

const homeCount = computed(() =>
  homeDecisionCount({ projects: store.state.projects, home: store.state.home, sessions: store.state.sessions, joins: Object.values(store.state.joins.items), leaves: Object.values(store.state.leaves.items), groupOps: Object.values(store.state.groupOps.items), reminders: Object.values(store.state.reminders.items) }),
);

// «Ждёт тебя · N» — тот же счёт, что в панели (`WaitBar`) и у вкладки «Ждёт» телефона.
const waitTotal = computed(() => waitSummary(buildWaiting(waitingInputOf(store)), boardState.layout.hidden_projects).total);

// Ф58-1: видимая кнопка «Написать…» со значком: непрочитанные реплики агента владельца + ответы агентов.
const writeUnread = computed(
  () => (store.state.owner_channel.agent.talk_unread ?? 0) + unreadRepliesCount(store.state.peerMsgs.items),
);

function isActive(tab: ProjectTab): boolean {
  return !store.state.homeOpen && !store.state.claudeOpen && store.state.currentProjectId === tab.id;
}

function openHome(): void {
  store.openHome();
}

function openTab(tab: ProjectTab): void {
  void store.openProject(tab.id);
}

// Активная вкладка — в видимую часть полосы (jsdom `scrollIntoView` не знает — без него просто ничего).
const tabsEl = ref<HTMLElement | null>(null);
watch(
  () => [store.state.homeOpen, store.state.claudeOpen, store.state.currentProjectId],
  async () => {
    await nextTick();
    const active = tabsEl.value?.querySelector<HTMLElement>('[aria-current="page"]');
    active?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  },
);
</script>

<template>
  <header class="top-bar shell-top-bar">
    <nav
      ref="tabsEl"
      class="project-tabs"
      aria-label="Проекты"
    >
      <button
        type="button"
        class="project-tab project-tab--home"
        :aria-current="store.state.homeOpen ? 'page' : undefined"
        @click="openHome"
      >
        <span class="project-tab-name">Главная</span>
        <span
          v-if="homeCount > 0"
          class="badge badge-react"
          title="ждут твоего решения: запросы сессий, предложенные задачи, черновики"
        >{{ homeCount }}</span>
      </button>
      <button
        v-for="tab in tabs"
        :key="tab.id"
        type="button"
        class="project-tab"
        :class="`project-tab--${tab.kind}`"
        :data-project-id="tab.id"
        :aria-current="isActive(tab) ? 'page' : undefined"
        @click="openTab(tab)"
      >
        <span
          v-if="tab.kind === 'project'"
          class="color-dot"
          :style="{ backgroundColor: safeColor(tab.color) }"
        />
        <span class="project-tab-name">{{ tab.name }}</span>
        <span
          v-if="tab.badge !== null && tab.badge.kind !== 'quiet'"
          class="badge"
          :class="`badge-${tab.badge.kind}`"
        >{{ tab.badge.count }}</span>
      </button>
    </nav>

    <div class="shell-status">
      <button
        v-if="store.state.homeOpen"
        type="button"
        class="top-wait-button"
        :class="{ 'top-wait-button--none': waitTotal === 0 }"
        :aria-pressed="boardState.waitShown"
        :title="waitTotal > 0 ? 'Ждёт тебя' : 'Ничего не ждёт'"
        data-test="top-wait"
        @click="toggleWait(boardState.waitShown ? null : 'all')"
      >
        {{ waitTotal > 0 ? `Ждёт тебя · ${waitTotal}` : 'Ничего не ждёт' }} ▾
      </button>
      <button
        type="button"
        class="top-write-button"
        title="Написать…"
        aria-label="Написать…"
        data-test="top-write"
        @click="openWrite()"
      >
        <span aria-hidden="true">✎</span><span class="top-write-label"> Написать…</span>
        <span
          v-if="writeUnread > 0"
          class="badge badge-react"
          data-test="top-write-badge"
          :title="`Непрочитанных ответов: ${writeUnread}`"
        >{{ writeUnread }}</span>
      </button>
      <button
        type="button"
        class="top-term-button"
        title="Терминал"
        aria-label="Терминал"
        :aria-pressed="termState.open && !termState.collapsed"
        data-test="top-term"
        @click="toggleTerminal()"
      >
        <span aria-hidden="true">⌨</span><span class="top-term-label"> Терминал</span>
      </button>
      <TopMenu />
    </div>
  </header>
</template>
