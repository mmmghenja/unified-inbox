<script setup lang="ts">
// Ф41-6б: четвёртая вкладка — «Сессии» (`PhoneSessions`), «Агенты» (бывшая «Диспетчерская») — страница без своей вкладки.
// Ф10-6 B4: телефон (< 600 px). Верх — узкая полоса: название раздела и меню «⋯» (`TopMenu.vue`; тревоги связи,
// аккаунтов и очереди — в полосе сами, Ф20-4). Низ — фиксированная панель «Ждёт · N | Проекты | Беседы | Сессии | Агент»
// (вкладка «Claude» — оркестратор и сессии по проектам, `ClaudeView`; без серых счётчиков на чипах). После
// входа — «Ждёт». «Назад» браузера/Android закрывает верхний уровень бесед (тред -> лента -> список): записи
// истории кладёт стор (`openChannel`/`openThread`), здесь — только слушатель `popstate`.
// Ф14-2 D1: в «Беседах» над чипами проектов — кнопка «Новое сообщение».
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import type { PhoneTab } from '../store/store';
import { store } from '../store/instance';
import { unreadRepliesCount } from '../store/peerMsgs';
import { safeColor } from '../util/color';
import { projectChips, type ProjectTab } from '../util/tabs';
import { buildWaiting } from '../util/waiting';
import { waitingInputOf } from '../util/waitInput';
import NewMessageButton from './NewMessageButton.vue';
import ChatsView from './ChatsView.vue';
import ClaudeView from './ClaudeView.vue';
import AgentQueue from './AgentQueue.vue';
import PhoneSessions from './PhoneSessions.vue';
import HomeView from './HomeView.vue';
import SearchBox from './SearchBox.vue';
import TopMenu from './TopMenu.vue';
import WaitList from './WaitList.vue';

const tab = computed(() => store.state.phoneTab);

// Ф90-1 (Р2): в открытой сессии и беседе верхняя полоса раздела не нужна (шапка окна своя); в открытой сессии и в беседе, пока
// поле ввода в фокусе (клавиатура открыта), нижние вкладки прячутся — лента получает всю высоту.
const sessionOpen = computed(() => tab.value === 'orch' && store.state.phoneSession !== null);
const channelOpen = computed(() => tab.value === 'chats' && store.state.currentChannelId !== null);
const typing = ref(false);
function isField(el: EventTarget | null): boolean {
  return el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement && ['text', 'search', ''].includes(el.type));
}
function onFocusIn(event: FocusEvent): void {
  if (isField(event.target)) typing.value = true;
}
function onFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget;
  // Фокус перешёл на кнопку в том же окне (тап по «➤», «📎») — вкладки не возвращаем: клавиатура всё ещё рядом.
  if (next instanceof HTMLElement && next.closest('.phone-body') !== null && !isField(next) && typing.value) return;
  typing.value = false;
}
const hideTabs = computed(() => typing.value && (sessionOpen.value || channelOpen.value));

const agentReplies = computed(() => unreadRepliesCount(store.state.peerMsgs.items));

const ownerCount = computed(() => store.state.owner_channel.items.length);

const waitCount = computed(
  () => buildWaiting(waitingInputOf(store)).length,
);

const TITLES: Record<PhoneTab, string> = {
  wait: 'Ждёт',
  projects: 'Проекты',
  chats: 'Беседы',
  orch: 'Сессии',
  agent: 'Агент',
  dispatch: 'Агенты',
};

const chips = computed(() => projectChips(store.state.projects));

function chipActive(chip: ProjectTab): boolean {
  return store.state.currentProjectId === chip.id;
}

function openChip(chip: ProjectTab): void {
  void store.openProject(chip.id);
}

function select(next: PhoneTab): void {
  store.setPhoneTab(next);
}

function onPopState(): void {
  store.onPhonePopState();
}

onMounted(() => {
  window.addEventListener('popstate', onPopState);
});

onBeforeUnmount(() => {
  window.removeEventListener('popstate', onPopState);
});
</script>

<template>
  <div
    class="phone-shell"
    :class="[`phone-shell--${tab}`, { 'phone-shell--session': sessionOpen, 'phone-shell--typing': hideTabs }]"
    @focusin="onFocusIn"
    @focusout="onFocusOut"
  >
    <header
      v-if="!sessionOpen && !channelOpen"
      class="phone-top"
    >
      <h1 class="phone-title">
        {{ TITLES[tab] }}
      </h1>
      <TopMenu phone />
    </header>

    <div class="phone-body">
      <WaitList v-if="tab === 'wait'" />
      <HomeView
        v-else-if="tab === 'projects'"
        phone
      />
      <div
        v-else-if="tab === 'chats'"
        class="phone-chats"
      >
        <div
          v-if="store.state.currentChannelId === null"
          class="phone-chats-head"
        >
          <SearchBox />
          <NewMessageButton />
          <nav
            class="project-chips"
            aria-label="Проекты"
          >
            <button
              v-for="chip in chips"
              :key="chip.id"
              type="button"
              class="project-chip"
              :class="`project-chip--${chip.kind}`"
              :data-project-id="chip.id"
              :aria-current="chipActive(chip) ? 'page' : undefined"
              @click="openChip(chip)"
            >
              <span
                v-if="chip.kind === 'project'"
                class="color-dot"
                :style="{ backgroundColor: safeColor(chip.color) }"
              />
              {{ chip.name }}
              <span
                v-if="chip.badge !== null && chip.badge.kind !== 'quiet'"
                class="badge"
                :class="`badge-${chip.badge.kind}`"
              >{{ chip.badge.count }}</span>
            </button>
          </nav>
        </div>
        <ChatsView />
      </div>
      <PhoneSessions v-else-if="tab === 'orch'" />
      <AgentQueue v-else-if="tab === 'agent'" />
      <ClaudeView
        v-else
        phone
      />
    </div>

    <nav
      v-show="!hideTabs"
      class="phone-tabs"
      aria-label="Разделы"
    >
      <button
        type="button"
        class="phone-tab phone-tab--wait"
        :aria-current="tab === 'wait' ? 'page' : undefined"
        @click="select('wait')"
      >
        Ждёт<template v-if="waitCount > 0">
          · <span class="badge badge-react">{{ waitCount }}</span>
        </template>
      </button>
      <button
        type="button"
        class="phone-tab phone-tab--projects"
        :aria-current="tab === 'projects' ? 'page' : undefined"
        @click="select('projects')"
      >
        Проекты
      </button>
      <button
        type="button"
        class="phone-tab phone-tab--chats"
        :aria-current="tab === 'chats' ? 'page' : undefined"
        @click="select('chats')"
      >
        Беседы
      </button>
      <button
        type="button"
        class="phone-tab phone-tab--orch"
        :aria-current="tab === 'orch' ? 'page' : undefined"
        @click="select('orch')"
      >
        Сессии<template v-if="agentReplies > 0">
          · <span class="badge badge-react agent-replies-badge">{{ agentReplies }}</span>
        </template>
      </button>
      <button
        type="button"
        class="phone-tab phone-tab--agent"
        :aria-current="tab === 'agent' ? 'page' : undefined"
        @click="select('agent')"
      >
        Агент<template v-if="ownerCount > 0">
          · <span class="badge badge-react owner-badge">{{ ownerCount }}</span>
        </template>
      </button>
    </nav>
  </div>
</template>
