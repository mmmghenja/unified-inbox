<script setup lang="ts">
// Ф9-2 B: лента событий задачи — последние 30, новые снизу; автопрокрутка только если лента уже внизу;
// «показать раньше» догружает по `before`. Ф10-1: событие `text` (ответ модели) — markdown через `ModelText`
// (длинное свёрнуто; последний ответ — всегда целиком); остальные события — только текстом (pre-wrap).
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import type { SessionEvent, WorkEvent } from '../api/types';
import { eventView } from '../util/dispatch';
import { formatTime } from '../util/home';
import { groupSteps, type FeedRow } from '../util/sessionSteps';
import ModelText from './ModelText.vue';
import SessionStep from './SessionStep.vue';

// Ф41-2б-2: источник данных — пропсами (сессии хаба). Ф78-1: событий задач по «Го» больше нет.
const props = withDefaults(
  defineProps<{
    events?: (WorkEvent | SessionEvent)[];
    olderDone?: boolean;
    load?: () => Promise<void>;
    loadOlder?: () => Promise<void>;
    /** Ф41-3а: сессия хаба — строки с `brief`/подробностями раскрываются по клику. */
    sessionId?: number;
    /** Ф72-1: проект сессии — для поиска MD-файла по относительному пути в тексте модели. */
    projectId?: number;
    engine?: string;
    /** Ф41-3б: признак изменения хвоста ленты (строка «пишет…»): рост хвоста прокручивает вниз, если лента была внизу. */
    tail?: string;
  }>(),
  // `undefined` явно: иначе Vue приводит отсутствующий булев проп к `false` и признак из стора задачи не читается.
  {
    events: undefined,
    olderDone: undefined,
    load: undefined,
    loadOlder: undefined,
    sessionId: undefined,
    projectId: undefined,
    engine: undefined,
    tail: undefined,
  },
);

const feed = ref<HTMLElement | null>(null);
const events = computed(() => props.events ?? []);
const olderDone = computed(() => props.olderDone ?? false);
const loadingOlder = ref(false);

/** Ф41-3а: строка с кратким описанием или подробностями (только у сессий хаба; без них — как прежде). */
function isStep(event: WorkEvent | SessionEvent): event is SessionEvent {
  const e = event as SessionEvent;
  return e.kind !== 'text' && (Boolean(e.brief) || e.has_detail === true);
}

/** Первая реплика передачи дел новой сессии («Передача дел. Ты продолжаешь работу…») — длинная; в ленте одной строкой. */
function isHandoffSay(event: WorkEvent | SessionEvent): boolean {
  return event.kind === 'say' && /^(?:Владелец|Оркестратор) \((?:next|later|now)\): Передача дел\./.test(event.summary ?? '');
}
/** Ф92-1 (A1): реплика, которую от имени владельца отправил его агент (`brief.by`). */
function byAgent(event: WorkEvent | SessionEvent): boolean {
  return event.kind === 'say' && ((event as SessionEvent).brief as { by?: unknown } | null | undefined)?.by === 'agent';
}
const handoffOpen = ref<Record<number, boolean>>({});

// Окно сессии хаба: шаги инструментов, идущие подряд, — одной свёрнутой строкой (раскрывается по клику).
// Дорожки задач по «Го» — как прежде, строка на событие.
type Row = FeedRow<WorkEvent | SessionEvent>;
const rows = computed<Row[]>(() =>
  props.sessionId !== undefined ? groupSteps(events.value) : events.value.map((event) => ({ type: 'event', event })),
);
// Раскрытость — по id событий пачки: догрузка раньше/новые шаги меняют границы пачки, раскрытая остаётся раскрытой.
const toolsOpen = ref<Record<number, boolean>>({});
function groupOpen(row: Row & { type: 'tools' }): boolean {
  return row.events.some((e) => toolsOpen.value[e.id] === true);
}
function toggleGroup(row: Row & { type: 'tools' }): void {
  const next = !groupOpen(row);
  for (const e of row.events) toolsOpen.value[e.id] = next;
}
/** Строки для вывода: пачка — заголовком и (раскрытая) своими событиями. */
const lines = computed(() =>
  rows.value.flatMap((row) =>
    row.type === 'event'
      ? [{ key: `e${row.event.id}`, group: null, event: row.event }]
      : [
          { key: `g${row.id}`, group: row, event: null },
          ...(groupOpen(row) ? row.events.map((event) => ({ key: `e${event.id}`, group: null, event })) : []),
        ],
  ),
);

// Владелец 12.10.2026: последний ответ не сворачивается; все прежние — свёрнуты.
const lastTextId = computed(() => {
  for (let i = events.value.length - 1; i >= 0; i -= 1) if (events.value[i]!.kind === 'text') return events.value[i]!.id;
  return null;
});
/** Ключ ответа: у окончательного `text` — ключ его блока (`ref`, он же ключ строки «пишет…»), иначе id события. */
function answerKey(event: WorkEvent | SessionEvent): string {
  return String((event as SessionEvent).ref ?? event.id);
}

const AT_BOTTOM_PX = 40;
const USER_SCROLL_MS = 1000;
const TOP_PX = 30;

function atBottom(): boolean {
  const el = feed.value;
  if (el === null) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= AT_BOTTOM_PX;
}

function scrollToBottom(): void {
  const el = feed.value;
  if (el !== null) el.scrollTop = el.scrollHeight;
}

// Низ держится, пока владелец сам не прокрутил вверх (10.10: «иногда прокручивается, иногда стоит, низ дёргается»).
// Прежде «была ли лента внизу» мерилось перед каждым обновлением: строка «пишет…» дорисовывается позже своего
// признака (раз в 250 мс, разметка), лента отставала больше порога и держать низ переставала. Теперь признак
// снимает ТОЛЬКО прокрутка владельца (колесо вверх, палец, полоса прокрутки, клавиши), возвращает — его же прокрутка
// до низа; а подгоняет низ любое изменение ленты (новые строки, рост текста, раскрытие шага, картинка, смена
// высоты окна) — наблюдателями, до отрисовки кадра.
let stick = true;
let userAt = 0;
let pointerHeld = false;

function userInput(): void {
  userAt = Date.now();
  release();
}
function onWheel(event: WheelEvent): void {
  userInput();
  if (event.deltaY < 0) stick = false; // сразу: иначе рост текста между колесом и scroll вернёт вниз
}
function onPointerDown(): void {
  pointerHeld = true;
  userInput();
}
function onPointerUp(): void {
  pointerHeld = false;
}
function onScroll(): void {
  if (atBottom()) stick = true;
  else if (pointerHeld || Date.now() - userAt < USER_SCROLL_MS) stick = false;
  // Ф90-1: доскролил до верха сам — раньше подгружается автоматически (кнопка «показать раньше» — запасная).
  const el = feed.value;
  const byUser = pointerHeld || Date.now() - userAt < USER_SCROLL_MS;
  if (el !== null && byUser && el.scrollTop <= TOP_PX && el.scrollHeight > el.clientHeight && !olderDone.value && !loadingOlder.value && events.value.length > 0) {
    void showOlder();
  }
}
// Владелец 12.10.2026: последний ответ выше окна показывается с начала — вниз владелец листает сам. Ответ, который
// помещается, — как прежде, низом ленты. После ответа допустимы только строки «ход окончен» (иначе он уже не
// последнее в ленте). Как только владелец прокрутил сам, этот ответ к началу больше не возвращается (`released`).
let released: string | null = null;

function tallAnswer(): { key: string; top: number } | null {
  const el = feed.value;
  if (el === null || el.clientHeight === 0) return null;
  let node = el.lastElementChild as HTMLElement | null;
  while (node !== null && node.dataset.kind === 'turn_end') node = node.previousElementSibling as HTMLElement | null;
  if (node === null) return null;
  const live = node.dataset.live === '1' && props.tail !== undefined;
  const key = live ? props.tail!.slice(0, props.tail!.lastIndexOf(':')) : node.dataset.answerKey;
  if (key === undefined || node.offsetHeight <= el.clientHeight) return null;
  return { key, top: el.scrollTop + node.getBoundingClientRect().top - el.getBoundingClientRect().top };
}
function release(): void {
  const answer = tallAnswer();
  if (answer !== null) released = answer.key;
}
function follow(): void {
  if (!stick) return;
  const el = feed.value;
  const answer = tallAnswer();
  if (el !== null && answer !== null && answer.key !== released) el.scrollTop = answer.top;
  else scrollToBottom();
}

let mutations: MutationObserver | null = null;
let sizes: ResizeObserver | null = null;
function watchFeed(): void {
  const el = feed.value;
  if (el === null) return;
  if (typeof MutationObserver !== 'undefined') {
    mutations = new MutationObserver(follow);
    mutations.observe(el, { childList: true, subtree: true, characterData: true });
  }
  if (typeof ResizeObserver !== 'undefined') {
    sizes = new ResizeObserver(follow); // высота самого окна: карточка запроса, поле ввода, размер столбца
    sizes.observe(el);
  }
  el.addEventListener('load', follow, true); // картинка в тексте догрузилась
  window.addEventListener('pointerup', onPointerUp);
  window.addEventListener('pointercancel', onPointerUp);
}
onBeforeUnmount(() => {
  mutations?.disconnect();
  sizes?.disconnect();
  feed.value?.removeEventListener('load', follow, true);
  window.removeEventListener('pointerup', onPointerUp);
  window.removeEventListener('pointercancel', onPointerUp);
});

const scrollKey = computed(() => `${events.value.length > 0 ? events.value[events.value.length - 1]!.id : 0}:${props.tail ?? ''}`);
watch(
  scrollKey,
  async () => {
    if (!stick) return;
    await nextTick();
    follow();
  },
  { flush: 'post' },
);

onMounted(async () => {
  if (props.load !== undefined) await props.load();
  await nextTick();
  follow();
  watchFeed();
});

async function showOlder(): Promise<void> {
  const el = feed.value;
  const before = el !== null ? el.scrollHeight - el.scrollTop : 0;
  stick = false; // читает раньше — низ не держим, пока сам не вернётся
  loadingOlder.value = true;
  try {
    if (props.loadOlder !== undefined) await props.loadOlder();
  } finally {
    loadingOlder.value = false;
  }
  await nextTick();
  // Догруженное сверху не сдвигает то, что владелец читал.
  if (el !== null) el.scrollTop = el.scrollHeight - before;
}
</script>

<template>
  <div class="work-events">
    <button
      v-if="!olderDone && events.length > 0"
      type="button"
      class="link-button work-events-older"
      :disabled="loadingOlder"
      @click="showOlder"
    >
      показать раньше
    </button>
    <ol
      ref="feed"
      class="work-events-list"
      aria-label="События сессии"
      @scroll.passive="onScroll"
      @wheel.passive="onWheel"
      @touchstart.passive="userInput"
      @touchmove.passive="userInput"
      @keydown="userInput"
      @pointerdown="onPointerDown"
    >
      <template
        v-for="line in lines"
        :key="line.key"
      >
        <li
          v-if="line.group !== null"
          class="work-event work-event--tool work-event--tools"
          :data-tools-id="line.group.id"
        >
          <button
            type="button"
            class="step-toggle tools-toggle"
            :aria-expanded="groupOpen(line.group)"
            @click="toggleGroup(line.group)"
          >
            <span class="step-mark">{{ groupOpen(line.group) ? '▾' : '▸' }}</span>
            <span class="step-text">{{ line.group.text }}</span>
            <span
              v-if="line.group.errors > 0"
              class="tools-errors"
            > · ✗ {{ line.group.errors }}</span>
          </button>
        </li>
        <li
          v-else-if="line.event !== null"
          class="work-event"
          :class="[`work-event--${eventView(line.event).tone}`, { 'work-event--nested': sessionId !== undefined && (line.event.kind === 'tool' || line.event.kind === 'tool_done') }]"
          :data-event-id="line.event.id"
          :data-kind="line.event.kind"
          :data-by="byAgent(line.event) ? 'agent' : undefined"
          :data-answer-key="line.event.id === lastTextId ? answerKey(line.event) : undefined"
          :title="formatTime(line.event.ts)"
        >
          <ModelText
            v-if="line.event.kind === 'text'"
            :text="eventView(line.event).text"
            :collapsible="line.event.id !== lastTextId"
            :session-id="props.sessionId"
            :project-id="props.projectId"
            runnable
          />
          <template v-else-if="isHandoffSay(line.event)">
            <template v-if="handoffOpen[line.event.id]">
              {{ eventView(line.event).text }}
            </template>
            <template v-else>
              передача дел от прежней сессии ·
              <button
                type="button"
                class="link-button handoff-say-toggle"
                @click="handoffOpen[line.event.id] = true"
              >
                показать
              </button>
            </template>
          </template>
          <SessionStep
            v-else-if="sessionId !== undefined && isStep(line.event)"
            :event="line.event"
            :session-id="sessionId"
            :engine="engine"
          />
          <template v-else>
            {{ eventView(line.event).text }}
          </template>
        </li>
      </template>
      <li
        v-if="events.length === 0 && tail === undefined"
        class="work-event work-event--note"
      >
        событий пока нет
      </li>
      <slot name="tail" />
    </ol>
  </div>
</template>
