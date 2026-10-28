<script setup lang="ts">
// Ф20-4 A2–A4 (`docs/architecture.md`): правая часть верхней полосы — одна кнопка «⋯» с меню, общая для
// десктопа (`TopBar`) и телефона (`PhoneShell`, `phone`). В меню: «Новое сообщение», «Поиск» (поле открывается под
// пунктами, фокус в нём), «Раскладка», «Доступ», «Движок: …» (те же панели, что прежде) и строка состояния «всё
// в порядке» — связь, «аккаунты ✓», спокойные счётчики очереди («ждёт: N»).
// Тревоги остаются в полосе сами, без меню: неисправный аккаунт и пауза отправки, нет связи или данные устарели,
// сбои очереди («неизвестно», `dead_letter`). Закрывается по клику мимо, по Esc и при выборе пункта.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import * as api from '../api/client';
import type { Account } from '../api/types';
import { autopilotUi } from '../store/autopilot';
import { openColumn, setView } from '../store/board';
import { store } from '../store/instance';
import { unreadRepliesCount } from '../store/peerMsgs';
import { openTerminal } from '../store/terminal';
import { openWrite } from '../store/writeModal';
import { ENGINE_LABEL } from '../util/engine';
import { formatTime } from '../util/home';
import { hotkeyBus } from '../util/hotkeys';
import { usePopup } from '../util/popup';
import { isShowcase } from '../util/showcase';
import AccessPanel from './AccessPanel.vue';
import AutopilotPanel from './AutopilotPanel.vue';
import EnginePanel from './EnginePanel.vue';
import MachinesPanel from './MachinesPanel.vue';
import MappingPanel from './MappingPanel.vue';
import MyDataPanel from './MyDataPanel.vue';
import NewMessageForm from './NewMessageForm.vue';
import PeoplePanel from './PeoplePanel.vue';
import RemindersPanel from './RemindersPanel.vue';
import QueueCounters from './QueueCounters.vue';
import SearchBox from './SearchBox.vue';
import StatusIndicator from './StatusIndicator.vue';

const props = withDefaults(defineProps<{ phone?: boolean }>(), { phone: false });
// Витрина: «Мои данные» и «Движок» (агент и факты — личное владельца) не показываются.
const showcase = isShowcase();

const root = ref<HTMLElement | null>(null);
const menuButton = ref<HTMLButtonElement | null>(null);
const menuPanel = ref<HTMLElement | null>(null);
const search = ref<InstanceType<typeof SearchBox> | null>(null);
const menuOpen = ref(false);
const searchOpen = ref(false);

// Панели: открыта ровно одна (как прежде кнопки полосы). Форма «Новое сообщение» — поверх всего.
const accessOpen = ref(false);
const mappingOpen = ref(false);
const peopleOpen = ref(false);
const machinesOpen = ref(false);
const myDataOpen = ref(false);
const remindersOpen = ref(false);
const engineOpen = ref(false);
const newOpen = ref(false);

// Проект по умолчанию для формы — проект открытой вкладки, если это настоящий проект (как `NewMessageButton`).
const defaultProjectId = computed(() => {
  const projectId = store.state.currentProjectId;
  return !store.state.homeOpen && !store.state.claudeOpen && projectId !== null && projectId > 0 ? projectId : null;
});

function closeMenu(): void {
  menuOpen.value = false;
  searchOpen.value = false;
}

function toggleMenu(): void {
  if (menuOpen.value) {
    closeMenu();
    return;
  }
  menuOpen.value = true;
}

function closePanels(): void {
  autopilotUi.open = false;
  accessOpen.value = false;
  mappingOpen.value = false;
  peopleOpen.value = false;
  machinesOpen.value = false;
  myDataOpen.value = false;
  remindersOpen.value = false;
  engineOpen.value = false;
}

function pickNew(): void {
  closeMenu();
  newOpen.value = true;
}

// Ф99-5: горячие клавиши («Alt+M», «/») просят открыть форму и поиск — состояние здесь локальное.
watch(
  () => hotkeyBus.newMessage,
  () => pickNew(),
);
watch(
  () => hotkeyBus.search,
  () => {
    menuOpen.value = true;
    void pickSearch();
  },
);

async function pickSearch(): Promise<void> {
  searchOpen.value = true;
  await nextTick();
  search.value?.focus();
}

// Ф56-3: окно «Агент» — на компьютере столбец «своего набора» (открывается, если его нет), на телефоне вкладка.
function pickAgent(): void {
  closeMenu();
  if (props.phone) {
    store.setPhoneTab('agent');
    return;
  }
  store.openHome();
  setView('own');
  openColumn({ kind: 'owner_channel' });
}

// Ф58-1: окно «Написать…» — разговор с агентом владельца и письмо любой сессии.
function pickWrite(): void {
  closeMenu();
  openWrite();
}

// Ф56-5б: панель терминала снизу (на телефоне — во весь экран).
function pickTerminal(): void {
  closeMenu();
  void openTerminal();
}

function pickDispatch(): void {
  closeMenu();
  store.openClaude();
}

function pickMapping(): void {
  closeMenu();
  const next = !mappingOpen.value;
  closePanels();
  mappingOpen.value = next;
}

function pickPeople(): void {
  closeMenu();
  const next = !peopleOpen.value;
  closePanels();
  peopleOpen.value = next;
}

function pickMachines(): void {
  closeMenu();
  const next = !machinesOpen.value;
  closePanels();
  machinesOpen.value = next;
}

// Ф82-1: «Мои данные» — личные данные агента владельца (общий набор и наборы проектов).
function pickMyData(): void {
  closeMenu();
  const next = !myDataOpen.value;
  closePanels();
  myDataOpen.value = next;
}

// Ф89-1: «Напоминания» — все открытые, по проектам.
function pickReminders(): void {
  closeMenu();
  const next = !remindersOpen.value;
  closePanels();
  remindersOpen.value = next;
}

function pickAccess(): void {
  closeMenu();
  const next = !accessOpen.value;
  closePanels();
  accessOpen.value = next;
}

function pickAutopilot(): void {
  closeMenu();
  const next = !autopilotUi.open;
  closePanels();
  autopilotUi.open = next;
}

function pickEngine(): void {
  closeMenu();
  const next = !engineOpen.value;
  closePanels();
  engineOpen.value = next;
}

// Ф80-1 A2: Esc, нажатие мимо и открытие другого меню закрывают это меню (общий механизм `util/popup`); высота
// ограничена экраном и меню прокручивается (A3).
usePopup({
  open: menuOpen,
  button: () => menuButton.value,
  panel: () => menuPanel.value,
  onClose: () => {
    searchOpen.value = false;
  },
});

// Ф11-4 И6: Telegram притормозил отправку (`flood_until` в будущем) — жёлтая пометка «пауза отправки до ЧЧ:ММ»;
// статус аккаунта при этом `ok`. Пропадает сама: часы тикают раз в 10 с.
const FLOOD_TICK_MS = 10_000;
const nowMs = ref(Date.now());
let floodTimer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  floodTimer = setInterval(() => {
    nowMs.value = Date.now();
  }, FLOOD_TICK_MS);
});
onBeforeUnmount(() => {
  if (floodTimer !== null) clearInterval(floodTimer);
});

const agentReplies = computed(() => unreadRepliesCount(store.state.peerMsgs.items));
const ownerCount = computed(() => store.state.owner_channel.items.length);
const writeUnread = computed(() => (store.state.owner_channel.agent.talk_unread ?? 0) + agentReplies.value);
const accounts = computed(() => store.state.status?.accounts ?? []);
const unhealthy = computed(() => accounts.value.filter((a) => a.status !== 'ok'));
const accountsTitle = computed(() => accounts.value.map((a) => `${a.title ?? a.source}: ${a.status}`).join('\n'));
const paused = computed(() =>
  accounts.value.filter((a) => a.flood_until !== null && a.flood_until > nowMs.value),
);

// Ф69-1: стоп PeerFlood — первые сообщения новым людям (`peer_flood_ts`) или вся отправка (`limited` + PeerFlood).
const stopped = computed(() =>
  accounts.value.filter(
    (a) => (a.peer_flood_ts ?? null) !== null || (a.status === 'limited' && a.last_error === 'PeerFlood'),
  ),
);
function stopText(account: Account): string {
  return (account.peer_flood_ts ?? null) !== null
    ? `Стоп: первые сообщения новым людям с ${formatTime(account.peer_flood_ts ?? 0)}`
    : 'Стоп: вся отправка (PeerFlood)';
}
const stopBusy = ref<number | null>(null);
async function resumeStop(account: Account): Promise<void> {
  const name = account.title ?? account.source;
  if (!window.confirm(`Telegram может снова отказать. Перед снятием проверь @SpamBot в Telegram этого номера (${name}).`)) return;
  stopBusy.value = account.id;
  try {
    await api.resumeAccount(account.id);
  } catch (err) {
    // 409 `nothing_to_resume` — стоп уже снят: тихо обновляем статус.
    if (!(err instanceof api.ApiError && err.status === 409)) throw err;
  } finally {
    stopBusy.value = null;
    await store.refreshStatus();
  }
}
// Ф66-3 F: хоть одна сессия хаба ждёт паузу лимита подписки — в меню «Снять паузу лимита».
const pausedSessions = computed(() =>
  Object.values(store.state.sessions.items).filter((session) => session.wait_reason === 'paused'),
);
const PAUSE_CONFIRM = 'Снять паузу у всех сессий? Если лимит ещё не сброшен, Claude снова его покажет.';
const pauseBusy = ref(false);
async function clearPause(): Promise<void> {
  closeMenu();
  if (!window.confirm(PAUSE_CONFIRM)) return;
  pauseBusy.value = true;
  try {
    await api.clearWorkPause();
  } finally {
    pauseBusy.value = false;
    void store.loadSessions();
  }
}
const accountsOk = computed(
  () => accounts.value.length > 0 && unhealthy.value.length === 0 && paused.value.length === 0 && stopped.value.length === 0,
);

// Связь «не в норме» — как в `StatusIndicator`: лента не обновляется или `/sync` не удался.
const connectionBad = computed(() => store.state.staleness.stale || store.state.syncError !== null);

// Ф16-1: движок из `/status`. Ф80-1 E1: с телефона переключается так же (запрет ядра снимает Ф78-2a).
// Ф78-2c: чип — «<движок> · <модель>» агента владельца (модель из `/status`, нет — только движок).
const engineLabel = computed(() => {
  const st = store.state.status;
  const name = ENGINE_LABEL[st?.engine ?? 'claude'];
  const model = st?.engine_model;
  return model === undefined || model === null || model === '' ? name : `${name} · ${model}`;
});
const engineTitle = computed(() => `Движок агента: ${engineLabel.value} — выбрать`);
</script>

<template>
  <div
    ref="root"
    class="top-menu"
    :class="{ 'top-menu--phone': props.phone }"
  >
    <!-- Тревоги — в полосе сами, без меню. -->
    <StatusIndicator
      v-if="connectionBad"
      :compact="props.phone"
    />
    <ul
      v-if="unhealthy.length > 0 || paused.length > 0 || stopped.length > 0"
      class="account-statuses"
      aria-label="Статусы аккаунтов"
    >
      <li
        v-for="account in unhealthy"
        :key="account.id"
        :class="`status-${account.status}`"
      >
        {{ account.title ?? account.source }}: {{ account.status }}
        <span v-if="account.status === 'auth_required' && account.last_error">— {{ account.last_error }}</span>
      </li>
      <li
        v-for="account in stopped"
        :key="`stop-${account.id}`"
        class="status-limited account-stop"
      >
        {{ account.title ?? account.source }}: {{ stopText(account) }}
        <button
          type="button"
          class="account-stop-resume"
          :disabled="stopBusy === account.id"
          @click="resumeStop(account)"
        >
          Снять стоп
        </button>
      </li>
      <li
        v-for="account in paused"
        :key="`flood-${account.id}`"
        class="status-stale account-flood"
      >
        {{ account.title ?? account.source }}: пауза отправки до {{ formatTime(account.flood_until ?? 0) }}
      </li>
    </ul>
    <QueueCounters
      part="alerts"
      :phone="props.phone"
    />

    <span class="top-menu-anchor">
      <button
        ref="menuButton"
        type="button"
        class="top-menu-button"
        aria-label="Меню"
        aria-haspopup="menu"
        :aria-expanded="menuOpen"
        @click="toggleMenu"
      >
        ⋯
      </button>
      <div
        v-if="menuOpen"
        ref="menuPanel"
        class="top-menu-popup"
        role="menu"
        aria-label="Меню"
      >
        <button
          type="button"
          role="menuitem"
          class="top-menu-item new-message-button"
          @click="pickNew"
        >
          Новое сообщение
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item top-menu-search"
          :aria-expanded="searchOpen"
          @click="pickSearch"
        >
          Поиск
        </button>
        <SearchBox
          v-if="searchOpen"
          ref="search"
          @picked="closeMenu"
        />
        <button
          type="button"
          role="menuitem"
          class="top-menu-item agent-open"
          title="Очередь владельца: разрешения, вопросы, черновики, ответы сессий"
          @click="pickAgent"
        >
          Агент
          <span
            v-if="ownerCount > 0"
            class="badge badge-react owner-badge"
            :title="`Ждёт тебя: ${ownerCount}`"
          >{{ ownerCount }}</span>
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item write-open"
          title="Написать агенту владельца или любой сессии"
          @click="pickWrite"
        >
          Написать…
          <span
            v-if="writeUnread > 0"
            class="badge badge-react write-badge"
            :title="`Непрочитанных ответов: ${writeUnread}`"
          >{{ writeUnread }}</span>
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item terminal-open"
          title="Терминал: настоящая оболочка, команды из ответов сессий"
          @click="pickTerminal"
        >
          Терминал
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item dispatch-open"
          title="Агенты и переписка между ними"
          @click="pickDispatch"
        >
          Агенты
          <span
            v-if="agentReplies > 0"
            class="badge badge-react agent-replies-badge"
            :title="`Ответов агентов, которых вы не прочитали: ${agentReplies}`"
          >{{ agentReplies }}</span>
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item mapping-open"
          title="Раскладка бесед по проектам: правила и проекты"
          @click="pickMapping"
        >
          Раскладка
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item people-open"
          title="Люди: знакомые, поиск, объединение дублей"
          @click="pickPeople"
        >
          Люди
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item machines-open"
          title="Машины сессий: имена, путь, версия"
          @click="pickMachines"
        >
          Машины
        </button>
        <button
          v-if="!showcase"
          type="button"
          role="menuitem"
          class="top-menu-item my-data-open"
          title="Мои данные: что агент вписывает в формы — общее и по проектам"
          data-test="my-data-open"
          @click="pickMyData"
        >
          Мои данные
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item reminders-open"
          title="Напоминания: все открытые, по проектам"
          data-test="reminders-open"
          @click="pickReminders"
        >
          Напоминания
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item autopilot-open"
          title="Что сессии в авто-режиме делают сами"
          @click="pickAutopilot"
        >
          Автопилот
        </button>
        <button
          v-if="pausedSessions.length > 0"
          type="button"
          role="menuitem"
          class="top-menu-item pause-clear"
          title="Лимит подписки уже сброшен — подать накопленные реплики и сигналы сейчас"
          data-test="pause-clear"
          :disabled="pauseBusy"
          @click="clearPause"
        >
          Снять паузу лимита
        </button>
        <button
          type="button"
          role="menuitem"
          class="top-menu-item access-open"
          title="Доступ с телефона и входы"
          @click="pickAccess"
        >
          Доступ
        </button>
        <button
          v-if="!showcase"
          type="button"
          role="menuitem"
          class="top-menu-item engine-chip"
          :title="engineTitle"
          data-testid="engine-chip"
          @click="pickEngine"
        >
          Движок: {{ engineLabel }}
        </button>
        <div
          class="top-menu-status"
          role="presentation"
        >
          <StatusIndicator />
          <span
            v-if="accountsOk"
            class="status-ok accounts-ok"
            :title="accountsTitle"
          >аккаунты ✓</span>
          <QueueCounters
            part="calm"
            :phone="props.phone"
          />
        </div>
      </div>
    </span>

    <div
      v-if="newOpen"
      class="new-message-overlay"
    >
      <NewMessageForm
        :project-id="defaultProjectId"
        @done="newOpen = false"
        @cancel="newOpen = false"
      />
    </div>
    <Teleport to="body">
      <AutopilotPanel
        v-if="autopilotUi.open"
        @close="autopilotUi.open = false"
      />
    </Teleport>
    <EnginePanel
      v-if="engineOpen && !showcase"
      @close="engineOpen = false"
    />
    <MappingPanel
      v-if="mappingOpen"
      @close="mappingOpen = false"
    />
    <PeoplePanel
      v-if="peopleOpen"
      @close="peopleOpen = false"
    />
    <MachinesPanel
      v-if="machinesOpen"
      @close="machinesOpen = false"
    />
    <MyDataPanel
      v-if="myDataOpen && !showcase"
      @close="myDataOpen = false"
    />
    <RemindersPanel
      v-if="remindersOpen"
      @close="remindersOpen = false"
    />
    <AccessPanel
      v-if="accessOpen"
      @close="accessOpen = false"
    />
  </div>
</template>
