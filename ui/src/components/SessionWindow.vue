<script setup lang="ts">
// Ф41-2б-2: окно сессии хаба (минимум) — карточки разрешений и вопросов, лента событий (последние 30), очередь
// неотправленных реплик, поле ввода («Отправить» в очередь и «Прервать и отправить»), кнопки «Прервать»,
// «Усыпить», «Закрыть» и «Открыть в терминале» (у спящей). Отказ ядра (409/410/422) — его русской строкой.
// Тексты владельца, события и причины — недоверенный ввод: только текстовая интерполяция.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { agentStateParts, dayNote } from '../util/agentState';
import { terminalMachineLine } from '../util/machines';

import type { HubSession } from '../api/types';
import { autopilotUi, catalogSize, loadAutopilot } from '../store/autopilot';
import { store } from '../store/instance';
import { AUTO_EQUALS_MANUAL_HINT, autoConfirmText, autoEqualsManual } from '../util/autopilot';
import type { SessionActionResult, WorkActionResult } from '../store/store';
import { copyText } from '../util/copy';
import { STALE_RETRY_TEXT } from '../util/dispatch';
import { restoreUnsent } from '../util/sendField';
import {
  SESSION_CLOSE_CONFIRM,
  SESSION_TERMINAL_NOTE,
  sessionTerminalCommand,
} from '../util/sessions';
import { sayQueueReason } from '../util/sayQueue';
import { switchLabel } from '../util/engineFallback';
import { SUB_APPLY_NOTE, buildWantSub, emptySubForm, heavyCount, heavyLine, PAUSED_LINE, pausedWaiting, subFormOf, subLine } from '../util/sessionSub';
import { onSubmitKey, showSubmitHint, submitKeyLabel } from '../util/submitKeys';
import SessionBgChip from './SessionBgChip.vue';
import DictateButton from './DictateButton.vue';
import FormFrame from './FormFrame.vue';
import { bgSleepConfirm } from '../util/sessionBg';
import SessionHandoffForm from './SessionHandoffForm.vue';
import InterviewPanel from './InterviewPanel.vue';
import SessionInterviewForm from './SessionInterviewForm.vue';
import { interviewUi, loadInterviewCurrent } from '../store/interview';
import { isPhone } from '../util/layout';
import { usePopup } from '../util/popup';
import { registerHotkeys } from '../util/hotkeys';
import SessionFallbackChip from './SessionFallbackChip.vue';
import SessionModelChip from './SessionModelChip.vue';
import RoleTextModal from './RoleTextModal.vue';
import SessionCompactModal from './SessionCompactModal.vue';
import SessionAlwaysModal from './SessionAlwaysModal.vue';
import SessionModelForm from './SessionModelForm.vue';
import SessionRoleForm from './SessionRoleForm.vue';
import { loadRoles, rolesState } from '../store/roles';
import { catalogRoleFor } from '../util/sessions';
import SessionSubFields from './SessionSubFields.vue';
import SessionFileChips from './SessionFileChips.vue';
import SessionLiveLine from './SessionLiveLine.vue';
import WorkEventFeed from './WorkEventFeed.vue';
import WorkRequestCard from './WorkRequestCard.vue';

// Ф41-6a: `column` — режим столбца «Главной»: лента занимает всю высоту и прокручивается внутри, поле ввода внизу.
const props = defineProps<{ session: HubSession; column?: boolean }>();

const sayText = ref('');
const sayButton = ref<HTMLButtonElement | null>(null);
const keyLabel = submitKeyLabel();
const showHint = showSubmitHint();
const busy = ref(false);
const error = ref<string | null>(null);
const showTerminal = ref(false);
const copied = ref(false);

const id = computed(() => props.session.id);
const running = computed(() => props.session.state === 'running');
const events = computed(() => store.state.sessions.events[id.value] ?? []);
const olderDone = computed(() => store.state.sessions.olderDone[id.value] === true);
const pending = computed(() => store.state.sessions.pending[id.value] ?? []);
const stale = computed(() => Object.values(store.state.sessions.stale).filter((r) => r.session_id === id.value));
const says = computed(() => store.state.sessions.says[id.value] ?? []);
// Ф99-4: при непустой очереди «Прервать» предупреждает, что реплики останутся.
const stopTitle = computed(() =>
  says.value.length > 0
    ? `Прервётся текущий ход; реплик в очереди: ${says.value.length} — останутся`
    : 'Прервать ход',
);
const queueReason = computed(() => sayQueueReason(props.session, pending.value, says.value));
const command = computed(() => sessionTerminalCommand(props.session));
// Ф77-1: на какой машине выполнять команду (машина сессии хаба — машина ядра).
const terminalMachine = computed(() => terminalMachineLine(props.session.machine));
// Ф41-4: строка подписки, правка (`PATCH want_sub`), «Поднять» и «Сжать».
const subInfo = computed(() => subLine(props.session));
const heavy = computed(() => heavyCount(props.session));
const paused = computed(() => pausedWaiting(props.session, says.value.length));
const editingSub = ref(false);
const subForm = reactive(emptySubForm());

function toggleSubEdit(): void {
  editingSub.value = !editingSub.value;
  if (editingSub.value) Object.assign(subForm, subFormOf(props.session.want_sub));
}

async function saveSub(): Promise<void> {
  const built = buildWantSub(subForm);
  if (!built.ok) {
    error.value = built.error;
    return;
  }
  if (await run(() => store.patchSessionSub(id.value, built.value))) editingSub.value = false;
}

// Ф41-7: «⋯» окна — «Модель и усилие…» и «Новая с передачей дел…» (формы раскрываются под строкой кнопок);
// закрытая с `close_reason: "handoff"` — последней строкой «дела переданы сессии #N».
// Ф80-1 C2: в столбце доски на компьютере формы — окном поверх, а не внутри узкого столбца.
const formModal = computed(() => props.column === true && !isPhone());
const moreOpen = ref(false);
const moreButton = ref<HTMLElement | null>(null);
const moreMenu = ref<HTMLElement | null>(null);
// Ф80-1 A2: общий механизм меню — Esc, нажатие мимо, одно открытое меню.
usePopup({ open: moreOpen, button: () => moreButton.value, panel: () => moreMenu.value });
const panel = ref<'model' | 'role' | 'handoff' | 'interview' | null>(null);
const roleTextOpen = ref(false);
const compactOpen = ref(false); // Ф48-2 D: окно «Сжатие…»
function openCompact(): void {
  moreOpen.value = false;
  compactOpen.value = true;
}
const alwaysOpen = ref(false); // Ф99-7.2: окно «Разрешения…» («Всегда в этой сессии»)
function openAlways(): void {
  moreOpen.value = false;
  alwaysOpen.value = true;
}
const catalogRole = computed(() => catalogRoleFor(props.session.role, rolesState.items));
// Ф48-3: окно есть у любой сессии — общий текст хаба и файл роли (или «Файл роли не задан»).
const roleTextTitle = computed(() => catalogRole.value?.title ?? props.session.role?.trim() ?? `Сессия #${props.session.id}`);
function openRoleText(): void {
  roleTextOpen.value = true;
}
onMounted(() => {
  if (!rolesState.loaded) void loadRoles();
});
const handoffTarget = computed(() => store.state.handoffTo[id.value] ?? null);

function openPanel(next: 'model' | 'role' | 'handoff' | 'interview'): void {
  moreOpen.value = false;
  panel.value = panel.value === next ? null : next;
}

// Ф41-6: серая строка состояния (режим столбца) тикает раз в секунду ради «кэш ММ:СС».
const nowTick = ref(Date.now());
let statusTimer: ReturnType<typeof setInterval> | null = null;
onMounted(() => {
  if (props.column) statusTimer = setInterval(() => (nowTick.value = Date.now()), 1000);
});
onBeforeUnmount(() => {
  if (statusTimer !== null) clearInterval(statusTimer);
});
const agentLine = computed(() => {
  const parts = agentStateParts(props.session, nowTick.value);
  // Ф64-1: ядро ещё не прислало `day_closed_ts` — день закрыт по проекту.
  const day = dayNote(props.session, store.state.projects);
  if (day !== null && !parts.includes(day)) parts.unshift(day);
  return parts.join(' · ');
});

// Ф41-5: переключатель «Ручной | Авто». Включение авто спрашивает подтверждение со счётом «придержано N из M» и
// ссылкой «Настроить» (панель «Автопилот»); возврат в ручной — сразу.
const isAuto = computed(() => props.session.mode === 'auto');
const confirmingAuto = ref(false);
const autoConfirm = computed(() => autoConfirmText(props.session.auto_held, catalogSize()));
// Подсказка: авто включён, а ни один выключатель не снят — по сути ручной режим.
const autoIsManual = computed(() => isAuto.value && autoEqualsManual(props.session.auto_held, catalogSize()));
if (props.session.mode === 'auto' && autopilotUi.data === null) void loadAutopilot();

async function chooseMode(next: 'manual' | 'auto'): Promise<void> {
  if (next === props.session.mode || busy.value) return;
  if (next === 'auto') {
    confirmingAuto.value = true;
    void loadAutopilot(); // M — длина каталога; ручки нет — подпись без счёта
    return;
  }
  confirmingAuto.value = false;
  await run(() => store.setSessionMode(id.value, 'manual'));
}

async function confirmAuto(): Promise<void> {
  if (await run(() => store.setSessionMode(id.value, 'auto'))) confirmingAuto.value = false;
}

function openAutopilot(): void {
  confirmingAuto.value = false;
  autopilotUi.open = true;
}

/** Заголовок карточки в авто: название выключателя, придержавшего запрос (в ручном — как прежде). */
function autoTitleOf(request: { rule_title?: string | null }): string | null {
  return isAuto.value && request.rule_title ? request.rule_title : null;
}

async function wake(): Promise<void> {
  await run(() => store.wakeSession(id.value));
}

// Ф83-3: «Перейти на <запасной>» у паузы и «Вернуть на основной» из «⋯» (409 «на паузе до …» — текстом ядра).
const switchText = computed(() => switchLabel(props.session));
async function switchFallback(): Promise<void> {
  await run(() => store.switchSessionFallback(id.value));
}
async function returnMain(): Promise<void> {
  await run(() => store.returnSessionFallback(id.value));
}

async function compact(): Promise<void> {
  await run(() => store.compactSession(id.value));
}

// Ф73-1: «Перечитать MCP» — процесс перезапускается после хода (спящую хост поднимет без хода).
async function mcpReload(): Promise<void> {
  await run(() => store.mcpReloadSession(id.value));
}

const hasText = computed(() => sayText.value.trim() !== '');

// Ф90-1: поле ввода растёт от одной строки до шести; «Прервать и отправить» — в меню у ➤ (и Ctrl+Shift+Enter).
const sayField = ref<HTMLTextAreaElement | null>(null);
const SAY_MAX_PX = 6 * 22;
function growSay(): void {
  const el = sayField.value;
  if (el === null || !props.column) return;
  el.style.height = 'auto';
  if (el.scrollHeight > 0) el.style.height = `${Math.min(el.scrollHeight, SAY_MAX_PX)}px`;
}
watch(sayText, () => void nextTick(growSay), { flush: 'post' });
onMounted(growSay);

// Ф99-5: «Alt+S» / «Alt+.» этого окна — когда фокус внутри него; иначе их берёт глобальная запись (первое окно).
function focusInsideThis(): boolean {
  const el = document.activeElement;
  return el instanceof Element && el.closest(`[data-session-id="${id.value}"]`) !== null;
}
const releaseHotkeys = registerHotkeys({
  keys: [
    {
      code: 'KeyS',
      alt: true,
      label: 'Alt+S',
      where: 'окно сессии',
      desc: 'фокус в поле ввода',
      when: focusInsideThis,
      run: () => {
        if (sayField.value === null) return false;
        sayField.value.focus();
      },
    },
    {
      code: 'Period',
      alt: true,
      label: 'Alt+.',
      where: 'окно сессии',
      desc: '«Прервать» ход',
      when: () => focusInsideThis() && running.value,
      run: () => {
        if (busy.value || props.session.interrupt_requested) return false;
        void interrupt();
      },
    },
  ],
});
onBeforeUnmount(releaseHotkeys);
const nowMenuOpen = ref(false);
const nowToggle = ref<HTMLElement | null>(null);
const nowMenu = ref<HTMLElement | null>(null);
usePopup({ open: nowMenuOpen, button: () => nowToggle.value, panel: () => nowMenu.value });

function onSayKey(event: KeyboardEvent): void {
  if (props.column && event.key === 'Enter' && event.shiftKey && (event.ctrlKey || event.metaKey) && !event.isComposing && !event.repeat) {
    event.preventDefault();
    if (running.value && canSay.value && !busy.value) void say('now');
    return;
  }
  onSubmitKey(event, sayButton.value);
}

// Ф90-1: карточки запросов в окне — максимум две видны, «ещё N» раскрывает остальные.
const MAX_CARDS = 2;
const showAllCards = ref(false);
const cardItems = computed(() => [
  ...pending.value.map((request) => ({ request, stale: false })),
  ...stale.value.map((request) => ({ request, stale: true })),
]);
const visibleCards = computed(() =>
  props.column && !showAllCards.value ? cardItems.value.slice(0, MAX_CARDS) : cardItems.value,
);
const hiddenCards = computed(() => cardItems.value.length - visibleCards.value.length);

// Ф41-3в: приложенное к реплике. Грузить можно несколько файлов сразу; пока что-то грузится — отправлять нельзя.
const files = computed(() => store.state.sessions.files[id.value] ?? []);
const uploading = computed(() => files.value.some((f) => f.status === 'uploading'));
const readyIds = computed(() => files.value.filter((f) => f.status === 'ready' && f.fileId !== null).map((f) => f.fileId!));
const canSay = computed(() => !uploading.value && (hasText.value || readyIds.value.length > 0));
const fileInput = ref<HTMLInputElement | null>(null);
const dragOver = ref(false);

function attach(list: FileList | File[] | null | undefined): void {
  if (!list || list.length === 0) return;
  const note = store.attachSessionFiles(id.value, Array.from(list));
  if (note !== null) error.value = note;
}

function onPick(event: Event): void {
  const input = event.target as HTMLInputElement;
  attach(input.files);
  input.value = '';
}

function onPaste(event: ClipboardEvent): void {
  const pasted = event.clipboardData?.files;
  if (!pasted || pasted.length === 0) return; // текст вставляется как обычно
  event.preventDefault();
  attach(pasted);
}

function hasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files');
}

function onDragOver(event: DragEvent): void {
  if (!hasFiles(event)) return;
  event.preventDefault();
  dragOver.value = true;
}

function onDrop(event: DragEvent): void {
  dragOver.value = false;
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  attach(event.dataTransfer.files);
}
// «Усыпить» нечего: сессия уже спит и реплик в очереди нет.
const nothingToSleep = computed(() => props.session.state === 'asleep' && says.value.length === 0 && props.session.queued_says === 0);

// Ф41-3б: строка «пишет…» — пока сессия в ходу и окончательный `text` этого блока ещё не пришёл в ленту.
const live = computed(() => {
  const l = store.state.sessions.live[id.value];
  if (l === undefined || (!running.value && !l.done)) return null; // законченный блок ждёт свой `text`
  if (events.value.some((e) => e.kind === 'text' && e.ref === l.block)) return null;
  return l;
});

let stopLive: (() => void) | null = null;
onMounted(() => {
  stopLive = store.watchSessionLive();
  // Ф60-1C: идущее интервью (панель) — один запрос на все окна; дальше кадры того же потока.
  if (!isPhone() && !interviewUi.loaded) void loadInterviewCurrent();
});
onBeforeUnmount(() => stopLive?.());

async function run(fn: () => Promise<SessionActionResult>): Promise<boolean> {
  busy.value = true;
  error.value = null;
  try {
    const result = await fn();
    if (!result.ok) error.value = result.message;
    return result.ok;
  } finally {
    busy.value = false;
  }
}

async function say(priority: 'next' | 'now'): Promise<void> {
  const text = sayText.value.trim();
  const attached = readyIds.value;
  if (!canSay.value) return;
  // Поле очищается сразу — следующее можно набирать, не дожидаясь ответа; не ушло — вернуть.
  sayText.value = '';
  let ok = false;
  try {
    ok = await run(() => store.saySession(id.value, text, priority, attached));
  } finally {
    if (!ok && text !== '') sayText.value = restoreUnsent(sayText.value, text);
  }
}

// Ф48-2 A: прерванная реплика — «Отправить снова» / «Убрать» (отказ 404/409 — текстом в окне).
async function resendSay(sayId: number): Promise<void> {
  await run(() => store.resendSessionSay(id.value, sayId));
}

async function dropSay(sayId: number): Promise<void> {
  await run(() => store.dropSessionSay(id.value, sayId));
}

async function interrupt(): Promise<void> {
  await run(() => store.interruptSession(id.value));
}

async function sleep(): Promise<void> {
  const warn = bgSleepConfirm(props.session);
  if (warn !== null && !window.confirm(warn)) return;
  await run(() => store.sleepSession(id.value));
}

async function close(): Promise<void> {
  if (!window.confirm(SESSION_CLOSE_CONFIRM)) return;
  await run(() => store.closeSession(id.value));
}

// Колбэки карточки запроса: решение — в `/session-requests/…`, «повтори» — репликой в очередь этой сессии.
function decide(
  request: (typeof pending.value)[number],
): (body: {
  decision: 'allow' | 'deny';
  answers?: Record<string, string>;
  message?: string;
  always?: boolean;
}) => Promise<WorkActionResult> {
  return (body) => store.decideSessionRequest(request, body);
}

async function retry(): Promise<WorkActionResult> {
  const result = await store.saySession(id.value, STALE_RETRY_TEXT, 'next');
  if (result.ok) return 'ok';
  error.value = result.message;
  return 'error';
}

async function openTerminal(): Promise<void> {
  if (command.value === null) return;
  showTerminal.value = true;
  if (await copyText(command.value)) {
    copied.value = true;
    setTimeout(() => (copied.value = false), 2000);
  }
}

// Ф41-6: действия для меню «⋯» шапки (столбец доски, окно на телефоне).
defineExpose({
  busy,
  canTerminal: computed(() => command.value !== null),
  nothingToSleep,
  sleep,
  wake,
  compact,
  mcpReload,
  returnMain,
  close,
  openPanel,
  openRoleText,
  openCompact,
  openAlways,
  openTerminal,
  toggleSubEdit,
  chooseMode,
  isAuto,
});
</script>

<template>
  <div
    class="work-task session-window"
    :class="{ 'session-window--drop': dragOver, 'session-window--column': column }"
    :data-session-id="session.id"
    @dragover="onDragOver"
    @dragleave="dragOver = false"
    @drop="onDrop"
  >
    <div
      v-if="!column"
      class="session-window-head"
    >
      <SessionModelChip
        :session="session"
        @click="openPanel('model')"
      />
      <SessionFallbackChip :session="session" />
    </div>
    <p
      v-if="session.failure"
      class="work-task-failure session-failure"
      :title="session.failure"
    >
      {{ session.failure }}
    </p>
    <div class="session-sub">
      <p
        v-if="!column"
        class="work-notice session-notice session-sub-line"
      >
        <span class="session-sub-text">{{ subInfo.text }}<template v-if="subInfo.state">
          · <span :class="session.listening === false ? 'session-sub-silent' : 'session-sub-ok'">{{ subInfo.state }}</span>
        </template></span>
        <button
          type="button"
          class="link-button session-sub-toggle"
          :aria-expanded="editingSub"
          @click="toggleSubEdit"
        >
          подписка ▾
        </button>
      </p>
      <p
        v-if="subInfo.stale"
        class="session-sub-stale"
      >
        {{ subInfo.stale }}
      </p>
      <p
        v-if="subInfo.autosend"
        class="session-sub-autosend"
      >
        {{ subInfo.autosend }}
      </p>
      <p
        v-if="heavy > 0"
        class="session-sub-heavy"
      >
        {{ heavyLine(heavy) }}
        <button
          type="button"
          class="session-wake"
          :disabled="busy"
          @click="wake"
        >
          Поднять
        </button>
        <button
          v-if="session.wait_reason === 'heavy'"
          type="button"
          class="session-handoff-heavy"
          :disabled="busy"
          @click="openPanel('handoff')"
        >
          Новая с передачей дел
        </button>
      </p>
      <p
        v-if="paused"
        class="session-sub-heavy session-paused"
      >
        {{ PAUSED_LINE }}
        <button
          type="button"
          class="session-wake-anyway"
          :disabled="busy"
          @click="wake"
        >
          Отправить всё равно
        </button>
        <button
          v-if="switchText !== null"
          type="button"
          class="session-fallback-switch"
          :disabled="busy"
          @click="switchFallback"
        >
          {{ switchText }}
        </button>
      </p>
      <FormFrame
        v-if="editingSub"
        :modal="formModal"
        title="Настройка подписки"
        @close="editingSub = false"
      >
        <div class="session-sub-edit">
          <SessionSubFields :form="subForm" />
          <p class="session-sub-note">
            {{ SUB_APPLY_NOTE }}
          </p>
          <div class="work-actions">
            <button
              type="button"
              class="session-sub-save"
              :disabled="busy"
              @click="saveSub"
            >
              Сохранить
            </button>
            <button
              type="button"
              class="link-button"
              @click="editingSub = false"
            >
              отмена
            </button>
          </div>
        </div>
      </FormFrame>
    </div>

    <div
      v-if="cardItems.length > 0"
      class="session-requests"
    >
      <WorkRequestCard
        v-for="item in visibleCards"
        :key="item.stale ? `stale-${item.request.id}` : item.request.id"
        :request="item.request"
        :stale="item.stale || undefined"
        :decide="decide(item.request)"
        :retry="retry"
        :dismiss="() => store.dismissStaleSessionRequest(item.request.id)"
        :auto-title="item.stale ? null : autoTitleOf(item.request)"
        :compact="item.stale ? undefined : true"
        :one-line="item.stale ? undefined : column === true"
      />
      <button
        v-if="hiddenCards > 0"
        type="button"
        class="link-button session-cards-more"
        @click="showAllCards = true"
      >
        ещё {{ hiddenCards }}
      </button>
    </div>

    <WorkEventFeed
      :events="events"
      :older-done="olderDone"
      :load="() => store.loadSessionEvents(id)"
      :load-older="() => store.loadOlderSessionEvents(id)"
      :session-id="id"
      :project-id="session.project_id"
      :engine="session.engine"
      :tail="live === null ? undefined : `${live.block}:${live.text.length}`"
    >
      <template #tail>
        <SessionLiveLine
          v-if="live !== null"
          :live="live"
        />
      </template>
    </WorkEventFeed>

    <ul
      v-if="says.length > 0"
      class="session-says"
      aria-label="Неотправленные реплики"
    >
      <li
        v-if="queueReason !== null"
        class="session-say-reason"
      >
        {{ queueReason }}
      </li>
      <li
        v-for="item in says"
        :key="item.id"
        class="session-say"
      >
        <span class="session-say-mark">{{ item.status === 'interrupted' ? 'в очереди' : item.priority === 'now' ? 'в очереди, срочно' : 'в очереди' }}:</span>
        {{ item.text }}
        <template v-if="item.status === 'interrupted'">
          <span class="session-say-interrupted">прервана</span>
          <button
            type="button"
            class="session-say-resend"
            :disabled="busy"
            @click="resendSay(item.id)"
          >
            Отправить снова
          </button>
          <button
            type="button"
            class="link-button session-say-drop"
            :disabled="busy"
            @click="dropSay(item.id)"
          >
            Убрать
          </button>
        </template>
        <span
          v-else-if="item.priority === 'now' && session.wait_reason === 'compact'"
          class="session-say-compact"
        >ждёт конца сжатия</span>
      </li>
    </ul>

    <InterviewPanel :session="session" />

    <p
      v-if="column && running && agentLine"
      class="session-statusline"
    >
      {{ agentLine }}
    </p>
    <div
      v-if="column"
      class="work-say work-say--bar"
    >
      <SessionFileChips
        :files="files"
        @remove="(localId) => store.removeSessionFile(id, localId)"
      />
      <div class="work-say-row">
        <button
          type="button"
          class="session-attach"
          title="Приложить файл"
          aria-label="Приложить файл"
          @click="fileInput?.click()"
        >
          📎
        </button>
        <textarea
          ref="sayField"
          v-model="sayText"
          class="work-say-text"
          rows="1"
          maxlength="16000"
          placeholder="Написать сессии"
          aria-label="Написать сессии"
          enterkeyhint="enter"
          @keydown="onSayKey"
          @input="growSay"
          @paste="onPaste"
        />
        <DictateButton />
        <button
          ref="sayButton"
          type="button"
          class="work-say-next"
          :title="`Отправить (${keyLabel})`"
          aria-label="Отправить"
          :disabled="busy || !canSay"
          @click="say('next')"
        >
          ➤
        </button>
        <button
          v-if="running"
          ref="nowToggle"
          type="button"
          class="work-say-more"
          title="Прервать и отправить (Ctrl+Shift+Enter)"
          aria-label="Ещё варианты отправки"
          :aria-expanded="nowMenuOpen"
          @click="nowMenuOpen = !nowMenuOpen"
        >
          ▾
        </button>
        <button
          v-if="running"
          type="button"
          class="session-stop"
          :title="stopTitle"
          aria-label="Стоп"
          :disabled="busy || session.interrupt_requested"
          @click="interrupt"
        >
          {{ session.interrupt_requested ? '…' : '⏹' }}
        </button>
        <div
          v-if="nowMenuOpen"
          ref="nowMenu"
          class="work-say-menu"
          role="menu"
        >
          <button
            type="button"
            role="menuitem"
            class="work-say-now"
            :disabled="busy || !canSay || !running"
            @click="nowMenuOpen = false; say('now')"
          >
            Прервать и отправить
          </button>
        </div>
      </div>
      <input
        ref="fileInput"
        type="file"
        multiple
        class="session-file-input"
        hidden
        @change="onPick"
      >
    </div>
    <div
      v-else
      class="work-say"
    >
      <textarea
        v-model="sayText"
        class="work-say-text"
        rows="2"
        maxlength="16000"
        placeholder="Написать сессии"
        aria-label="Написать сессии"
        enterkeyhint="enter"
        @keydown="onSubmitKey($event, sayButton)"
        @paste="onPaste"
      />
      <DictateButton />
      <SessionFileChips
        :files="files"
        @remove="(localId) => store.removeSessionFile(id, localId)"
      />
      <div class="work-actions">
        <button
          ref="sayButton"
          type="button"
          class="work-say-next"
          :title="keyLabel"
          :disabled="busy || !canSay"
          @click="say('next')"
        >
          Отправить
        </button>
        <span
          v-if="showHint"
          class="submit-hint"
          aria-hidden="true"
        >{{ keyLabel }}</span>
        <button
          v-if="!column || running"
          type="button"
          class="work-say-now"
          :disabled="busy || !canSay || !running"
          @click="say('now')"
        >
          Прервать и отправить
        </button>
        <button
          type="button"
          class="session-attach"
          title="Приложить файл"
          @click="fileInput?.click()"
        >
          📎 Файл
        </button>
        <button
          v-if="column && running"
          type="button"
          class="session-stop"
          :title="stopTitle"
          :disabled="busy || session.interrupt_requested"
          @click="interrupt"
        >
          {{ session.interrupt_requested ? 'прерываю…' : 'Стоп' }}
        </button>
        <input
          ref="fileInput"
          type="file"
          multiple
          class="session-file-input"
          hidden
          @change="onPick"
        >
      </div>
    </div>

    <div
      v-if="!column"
      class="session-mode"
      role="group"
      aria-label="Режим сессии"
    >
      <button
        type="button"
        class="session-mode-manual"
        :aria-pressed="!isAuto"
        :disabled="busy"
        @click="chooseMode('manual')"
      >
        Ручной
      </button>
      <button
        type="button"
        class="session-mode-auto"
        :aria-pressed="isAuto"
        :disabled="busy"
        @click="chooseMode('auto')"
      >
        Авто
      </button>
    </div>
    <p
      v-if="autoIsManual"
      class="session-auto-hint mapping-hint"
    >
      {{ AUTO_EQUALS_MANUAL_HINT }}
    </p>
    <div
      v-if="confirmingAuto"
      class="session-auto-confirm"
      role="alertdialog"
      aria-label="Включить авто"
    >
      <p>{{ autoConfirm }}</p>
      <div class="work-actions">
        <button
          type="button"
          class="session-auto-yes"
          :disabled="busy"
          @click="confirmAuto"
        >
          Включить авто
        </button>
        <button
          type="button"
          class="link-button session-auto-settings"
          @click="openAutopilot"
        >
          Настроить
        </button>
        <button
          type="button"
          class="link-button session-auto-cancel"
          @click="confirmingAuto = false"
        >
          отмена
        </button>
      </div>
    </div>

    <SessionBgChip
      v-if="!column"
      :session="session"
    />
    <div
      v-if="!column"
      class="work-actions work-controls"
    >
      <button
        type="button"
        class="session-interrupt"
        :disabled="busy || !running || session.interrupt_requested"
        @click="interrupt"
      >
        {{ session.interrupt_requested ? 'прерываю…' : 'Прервать' }}
      </button>
      <button
        type="button"
        class="session-sleep"
        :disabled="busy || session.sleep_requested || nothingToSleep"
        @click="sleep"
      >
        {{ session.sleep_requested ? 'усыпляю…' : 'Усыпить' }}
      </button>
      <button
        type="button"
        class="session-compact"
        title="Сжать контекст"
        :disabled="busy || session.state !== 'idle'"
        @click="compact"
      >
        Сжать
      </button>
      <button
        type="button"
        class="work-close session-close"
        :disabled="busy"
        @click="close"
      >
        Закрыть
      </button>
      <button
        v-if="command !== null"
        type="button"
        class="link-button session-terminal"
        @click="openTerminal"
      >
        Открыть в терминале
      </button>
    </div>
    <div
      v-if="showTerminal && command !== null"
      class="work-terminal"
    >
      <p
        v-if="terminalMachine"
        class="work-terminal-machine"
        data-test="terminal-machine"
      >
        {{ terminalMachine }}
      </p>
      <code class="work-terminal-command">{{ command }}</code>
      <span v-if="copied">скопировано</span>
      <p class="work-terminal-note">
        {{ SESSION_TERMINAL_NOTE }}
      </p>
    </div>
    <div
      v-if="!column"
      class="session-more"
    >
      <button
        ref="moreButton"
        type="button"
        class="link-button session-more-toggle"
        aria-label="Ещё действия"
        :aria-expanded="moreOpen"
        @click="moreOpen = !moreOpen"
      >
        ⋯
      </button>
      <div
        v-if="moreOpen"
        ref="moreMenu"
        class="session-more-menu"
        role="menu"
      >
        <button
          type="button"
          role="menuitem"
          class="session-more-model"
          @click="openPanel('model')"
        >
          Модель и усилие…
        </button>
        <button
          type="button"
          role="menuitem"
          class="session-more-compact"
          :disabled="session.closed_ts !== null"
          @click="openCompact()"
        >
          Сжатие…
        </button>
        <button
          type="button"
          role="menuitem"
          class="session-more-always"
          @click="openAlways()"
        >
          Разрешения…
        </button>
        <button
          type="button"
          role="menuitem"
          class="session-more-role"
          @click="openPanel('role')"
        >
          Роль…
        </button>
        <button
          type="button"
          role="menuitem"
          class="session-more-role-text"
          @click="moreOpen = false; openRoleText()"
        >
          Инструкция роли
        </button>
        <button
          type="button"
          role="menuitem"
          class="session-more-handoff"
          @click="openPanel('handoff')"
        >
          Новая с передачей дел…
        </button>
        <button
          v-if="!isPhone()"
          type="button"
          role="menuitem"
          class="session-more-interview"
          :disabled="session.closed_ts !== null"
          @click="openPanel('interview')"
        >
          Интервью…
        </button>
      </div>
    </div>
    <FormFrame
      v-if="panel === 'model'"
      :modal="formModal"
      title="Модель и усилие"
      @close="panel = null"
    >
      <SessionModelForm
        :session="session"
        @done="panel = null"
        @cancel="panel = null"
      />
    </FormFrame>
    <FormFrame
      v-if="panel === 'role'"
      :modal="formModal"
      title="Роль сессии"
      @close="panel = null"
    >
      <SessionRoleForm
        :session="session"
        @done="panel = null"
        @cancel="panel = null"
      />
    </FormFrame>
    <SessionCompactModal
      v-if="compactOpen"
      :session="session"
      @close="compactOpen = false"
    />
    <SessionAlwaysModal
      v-if="alwaysOpen"
      :session="session"
      @close="alwaysOpen = false"
    />
    <RoleTextModal
      v-if="roleTextOpen"
      :session-id="session.id"
      :title="roleTextTitle"
      @close="roleTextOpen = false"
    />
    <FormFrame
      v-if="panel === 'handoff'"
      :modal="formModal"
      title="Новая с передачей дел"
      @close="panel = null"
    >
      <SessionHandoffForm
        :session="session"
        @done="panel = null"
        @cancel="panel = null"
      />
    </FormFrame>
    <FormFrame
      v-if="panel === 'interview' && !isPhone()"
      :modal="formModal"
      title="Интервью"
      @close="panel = null"
    >
      <SessionInterviewForm
        :session="session"
        @done="panel = null"
        @cancel="panel = null"
      />
    </FormFrame>
    <p
      v-if="session.close_reason === 'handoff'"
      class="session-handoff-line"
    >
      дела переданы сессии{{ handoffTarget !== null ? ` #${handoffTarget}` : '' }}
    </p>
    <p
      v-if="error"
      class="work-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
