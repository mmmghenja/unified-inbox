<script setup lang="ts">
// Ф56-3: карточка очереди владельца (`docs/architecture.md`). Строка пути, метка вида, время,
// пометки «у агента» / «передал …» / «решает агент …», `brief`, «развернуть» (внутри для короткого, окном для
// длинного), кнопки ответов из `answers[]` и ряд «Поручить агенту». Ф58-3: пока поручение открыто — строка
// «решает агент: …» и «Остановить агента». Ф65-1: ответ и кнопки владельца видны ВСЕГДА (его ответ закрывает поручение сам);
// агент вернул — блок возврата с подробностями и полем «сказать агенту»; открытый вопрос агента — в карточке. Все тексты — чужой ввод (маскирован ядром):
// только текстовой интерполяцией, `url` — простым текстом с «копировать», скриншот — миниатюрой.
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue';

import type { OwnerAnswer, OwnerItem } from '../api/types';
import { store } from '../store/instance';
import { openWrite } from '../store/writeModal';
import { answerableGone, goneNotice, holdCard, nextHoldOwner } from '../store/ownerHold';
import { copyText } from '../util/copy';
import { isShowcase } from '../util/showcase';
import { onSubmitKey, submitKeyLabel } from '../util/submitKeys';
import { nextAnswerSeq } from '../util/writeTargets';
import {
  ageLabel,
  errandLine,
  fullLength,
  hasFull,
  isUrgentOwner,
  kindLabel,
  passedLine,
  pathLine,
  questionKey,
  untilLabel,
  OWNER_INLINE_MAX,
  ownerQuestions,
} from '../util/owner_channel';
import SessionFileChips from './SessionFileChips.vue';
import ErrandHands from './ErrandHands.vue';
import ErrandReturned from './ErrandReturned.vue';
import HumanShotModal from './HumanShotModal.vue';
import DictateButton from './DictateButton.vue';
import OwnerFullBody from './OwnerFullBody.vue';
import OwnerFullModal from './OwnerFullModal.vue';

const props = defineProps<{
  item: OwnerItem;
  nowMs: number;
  agentOnline: boolean;
  /** Ядро убрало карточку из очереди, а владелец с ней работает (`store/ownerHold.ts`). */
  gone?: boolean;
  /** Что пришло на смену ушедшей: карточка той же сессии. */
  successor?: OwnerItem | null;
}>();

// Ф58-1 C: id контейнера ответа в окне «открыть окном» (блок ответов переезжает туда `Teleport`-ом, состояние общее).
const answerId = `${props.item.ref.replace(/[^A-Za-z0-9_-]/g, '_')}-${nextAnswerSeq()}`;
const answerTarget = `#owner-modal-answer-${answerId}`;


const busy = ref(false);
const error = ref<string | null>(null);
const expanded = ref(false);
const modalOpen = ref(false);
// Блок ответов переезжает в окно только когда контейнер окна уже есть (после его монтирования) и возвращается
// раньше, чем окно уходит. Пока не переехал, `to` указывает на body: у отключённого Teleport цель не нужна.
const answerReady = ref(false);
const answerTo = computed(() => (answerReady.value ? answerTarget : 'body'));
watch(modalOpen, async (open) => {
  if (!open) {
    answerReady.value = false;
    return;
  }
  await nextTick();
  if (modalOpen.value) answerReady.value = true;
});
const shotOpen = ref(false);
const keyLabel = submitKeyLabel();
const sendButton = ref<HTMLButtonElement | null>(null);
const answerButton = ref<HTMLButtonElement | null>(null);

// Текстовое поле под карточкой: ответ с обязательным текстом или поручение агенту с текстом.
const textFor = ref<{ mode: 'answer' | 'errand'; action: string; label: string } | null>(null);
const textValue = ref('');
const textArea = ref<HTMLTextAreaElement | null>(null);

/** Открыть поле текста под кнопкой. Повторное нажатие той же кнопки (на телефоне её легко задеть — она
 *  называется так же, как кнопка отправки под полем) набранное НЕ стирает: только ставит курсор в поле.
 *  Текст стирается лишь после успешной отправки. */
function openText(mode: 'answer' | 'errand', action: string, label: string): void {
  const cur = textFor.value;
  if (cur === null || cur.mode !== mode || cur.action !== action) textFor.value = { mode, action, label };
  void nextTick(() => textArea.value?.focus());
}

// Файлы к ответу: ядро принимает их только у реплики сессии карточки (turn/reply; broken/dead/stuck/stall — say/interrupt).
const FILE_STATE_KINDS = ['broken', 'dead', 'stuck', 'stall'];
const canAttach = computed(() => {
  const t = textFor.value;
  const sid = props.item.session_id;
  if (t === null || t.mode !== 'answer' || sid === null) return false;
  if (props.item.kind === 'turn') return t.action === 'reply';
  return FILE_STATE_KINDS.includes(props.item.kind) && (t.action === 'say' || t.action === 'interrupt');
});
const files = computed(() => (props.item.session_id === null ? [] : (store.state.sessions.files[props.item.session_id] ?? [])));
const uploading = computed(() => files.value.some((f) => f.status === 'uploading'));
const readyIds = computed(() => files.value.filter((f) => f.status === 'ready' && f.fileId !== null).map((f) => f.fileId!));
const fileInput = ref<HTMLInputElement | null>(null);
const dragOver = ref(false);

function attach(list: FileList | File[] | null | undefined): void {
  const sid = props.item.session_id;
  if (!canAttach.value || sid === null || !list || list.length === 0) return;
  const note = store.attachSessionFiles(sid, Array.from(list));
  if (note !== null) error.value = note;
}
function onPick(event: Event): void {
  const input = event.target as HTMLInputElement;
  attach(input.files);
  input.value = '';
}
function onPaste(event: ClipboardEvent): void {
  const pasted = event.clipboardData?.files;
  if (!canAttach.value || !pasted || pasted.length === 0) return; // текст вставляется как обычно
  event.preventDefault();
  attach(pasted);
}
function onDragOver(event: DragEvent): void {
  if (!canAttach.value || !Array.from(event.dataTransfer?.types ?? []).includes('Files')) return;
  event.preventDefault();
  dragOver.value = true;
}
function onDrop(event: DragEvent): void {
  dragOver.value = false;
  if (!canAttach.value || !event.dataTransfer?.files?.length) return;
  event.preventDefault();
  attach(event.dataTransfer.files);
}
const comment = ref('');
const draftText = ref(typeof props.item.full.text === 'string' ? props.item.full.text : '');

const questions = computed(() => ownerQuestions(props.item.full));
const hasQuestions = computed(() => (props.item.kind === 'question') && questions.value.length > 0);
const isDraft = computed(() => props.item.kind === 'draft');
const isHuman = computed(() => props.item.kind === 'human');
const shot = computed(() => props.item.full.shot !== null && typeof props.item.full.shot === 'object');
const humanId = computed(() => (typeof props.item.full.id === 'number' ? props.item.full.id : null));
const shotSrc = computed(() => (humanId.value === null ? '' : `/api/v1/human-requests/${humanId.value}/shot`));
const sendAt = computed(() => props.item.full.send_at_ts ?? props.item.full.send_at ?? props.item.full.outbox_send_at_ts ?? null);

const urgent = computed(() => isUrgentOwner(props.item, props.nowMs));
const until = computed(() => untilLabel(props.item));
const age = computed(() => ageLabel(props.item.created_ts, props.nowMs));
const passed = computed(() => passedLine(props.item));
// Витрина: агента владельца нет — ни поручений («Поручить агенту», ход, возврат), ни его вопросов, ни разговора.
const showcase = isShowcase();
const errand = computed(() => (showcase ? null : errandLine(props.item.errand, props.nowMs)));
const returned = computed(() => (showcase || props.item.errand !== null ? null : (props.item.errand_returned ?? null)));
const ask = computed(() => (showcase ? null : (props.item.agent_ask ?? null)));
const askText = ref('');
const askButton = ref<HTMLButtonElement | null>(null);
const withAgent = computed(() => props.item.in_channel && props.agentOnline);
const inChannelOnly = computed(() => props.item.in_channel && !props.agentOnline);
// Для человека поля видны всегда; «развернуть» нужен прочим видам.
const foldable = computed(() => !isHuman.value && !hasQuestions.value && !isDraft.value);
const short = computed(() => fullLength(props.item, props.nowMs) <= OWNER_INLINE_MAX);
// Под «развернуть» есть что показать сверх `brief` — иначе ссылки нет (круг 3: без дублей brief/full).
const more = computed(() => hasFull(props.item, props.nowMs));
const title = computed(() => `${kindLabel(props.item.kind)} · ${pathLine(props.item)}`);
const commentAnswers = computed(() => !isDraft.value && props.item.answers.some((a) => a.takes_text && !a.needs_text));

// Выбранные варианты по номеру вопроса и «свой ответ».
const picked = reactive<Record<number, string[]>>({});
const custom = reactive<Record<number, string>>({});

function toggleOption(n: number, label: string, multi: boolean): void {
  const list = picked[n] ?? [];
  if (multi) picked[n] = list.includes(label) ? list.filter((x) => x !== label) : [...list, label];
  else picked[n] = list.includes(label) ? [] : [label];
}

const answersMap = computed<Record<string, string>>(() => {
  const result: Record<string, string> = {};
  for (const q of questions.value) {
    const own = (custom[q.n] ?? '').trim();
    const value = own !== '' ? own : (picked[q.n] ?? []).join(', ');
    if (value !== '') result[questionKey(q)] = value;
  }
  return result;
});
// Владелец 15.10.2026: карточку, которую смотрят или по которой пишут, не убирать вместе с очередью (`ownerHold`).
const holdOwner = nextHoldOwner();
const focused = ref(false);
// Ответили или закрыли ушедшую карточку — держать больше не нужно.
const done = ref(false);
const originalDraft = computed(() => (typeof props.item.full.text === 'string' ? props.item.full.text : ''));
const typedParts = computed(() => {
  const parts = [textValue.value, comment.value, ...questions.value.map((q) => custom[q.n] ?? '')];
  if (isDraft.value && draftText.value !== originalDraft.value) parts.unshift(draftText.value);
  return parts.map((x) => x.trim()).filter((x) => x !== '');
});
const engaged = computed(
  () =>
    !done.value &&
    (modalOpen.value ||
      expanded.value ||
      shotOpen.value ||
      focused.value ||
      textFor.value !== null ||
      typedParts.value.length > 0 ||
      Object.values(picked).some((list) => list.length > 0)),
);
watch(
  [engaged, () => props.item],
  ([on, item]) => holdCard(item.ref, holdOwner, on ? item : null),
  { immediate: true },
);
onBeforeUnmount(() => holdCard(props.item.ref, holdOwner, null));
// Снова открыл окно или начал писать после ответа — карточка снова «в работе».
watch([modalOpen, () => typedParts.value.length], ([open, n], [wasOpen, wasN]) => {
  if ((open && !wasOpen) || n > wasN) done.value = false;
});
// Ушедшая карточка: ответить можно только по ходу сессии (ядро примет репликой); прочие — только текст и выход.
const blocked = computed(() => props.gone === true && !answerableGone(props.item));
const notice = computed(() => (props.gone === true ? goneNotice(props.item, props.successor ?? null) : null));
const copied = ref(false);

function onFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget;
  if (!(next instanceof Node) || !(event.currentTarget as HTMLElement).contains(next)) focused.value = false;
}

async function copyTyped(): Promise<void> {
  copied.value = await copyText(typedParts.value.join('\n\n'));
}

async function toNew(): Promise<void> {
  const target = props.successor;
  if (target === null || target === undefined) return;
  modalOpen.value = false;
  await nextTick();
  const el = document.querySelector<HTMLElement>(`.owner-card[data-ref="${CSS.escape(target.ref)}"]`);
  if (el === null) return;
  el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  el.classList.add('owner-card--flash');
  setTimeout(() => el.classList.remove('owner-card--flash'), 2000);
}

// Владелец 15.10.2026: с карточки — в полный разговор с сессией, которая её прислала (окно «Написать…» поверх всего).
function openSession(): void {
  if (props.item.session_id === null) return;
  modalOpen.value = false;
  openWrite({ kind: 'session', id: props.item.session_id });
}

function closeGone(): void {
  modalOpen.value = false;
  done.value = true;
}

const canAnswer = computed(() => questions.value.length > 0 && Object.keys(answersMap.value).length === questions.value.length);

function disabled(a: OwnerAnswer): boolean {
  if (busy.value || blocked.value) return true;
  if (a.needs_answers === true) return !canAnswer.value;
  return false;
}

async function send(body: { action: string; text?: string; answers?: Record<string, string>; send_now?: boolean; files?: string[] }): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.answerOwner(props.item, body);
    if (!result.ok) {
      if (result.stale) store.state.owner_channel.notice = `${result.message} «${props.item.brief.slice(0, 60)}»`;
      else error.value = result.message;
    } else {
      textFor.value = null;
      textValue.value = '';
      // Ушли — чипы убираются (локально: файлы ядро уже забрало); не ушли — остаются.
      if (body.files !== undefined && props.item.session_id !== null) {
        const sid = props.item.session_id;
        const list = store.state.sessions.files[sid];
        if (list !== undefined) store.state.sessions.files[sid] = list.filter((f) => f.fileId === null || !body.files!.includes(f.fileId));
      }
      done.value = true;
      if (props.gone === true) modalOpen.value = false;
    }
  } finally {
    busy.value = false;
  }
}

function pressAnswer(a: OwnerAnswer): void {
  if (a.needs_text) {
    openText('answer', a.action, a.label);
    return;
  }
  const body: { action: string; text?: string; answers?: Record<string, string>; send_now?: boolean } = { action: a.action };
  if (a.needs_answers === true) body.answers = { ...answersMap.value };
  if (isDraft.value && a.action === 'approve') {
    const original = typeof props.item.full.text === 'string' ? props.item.full.text : '';
    if (draftText.value !== original && draftText.value.trim() !== '') body.text = draftText.value;
  } else if (a.takes_text === true && comment.value.trim() !== '') {
    body.text = comment.value.trim();
  }
  void send(body);
}

function sendNow(): void {
  const body: { action: string; text?: string; send_now: boolean } = { action: 'approve', send_now: true };
  const original = typeof props.item.full.text === 'string' ? props.item.full.text : '';
  if (draftText.value !== original && draftText.value.trim() !== '') body.text = draftText.value;
  void send(body);
}

async function submitText(): Promise<void> {
  const target = textFor.value;
  const value = textValue.value.trim();
  const attached = canAttach.value ? readyIds.value : [];
  if (target === null || (value === '' && attached.length === 0) || uploading.value) return;
  if (props.gone === true && (blocked.value || target.mode === 'errand')) return;
  if (target.mode === 'answer') {
    await send({ action: target.action, ...(value !== '' ? { text: value } : {}), ...(attached.length > 0 ? { files: attached } : {}) });
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    const result = await store.startErrand(props.item, { action: target.action, text: value });
    if (!result.ok) error.value = result.message;
    else {
      textFor.value = null;
      textValue.value = '';
    }
  } finally {
    busy.value = false;
  }
}

async function errandAction(id: string, needsText: boolean, label: string): Promise<void> {
  if (needsText) {
    openText('errand', id, label);
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    const result = await store.startErrand(props.item, { action: id });
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}

// Ф65-1 (И2): «Поручить агенту» (было «Пусть решает») — агент снова берёт карточку; текст — его указание (пусто — обычное «Реши»).
async function letDecide(text: string): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.startErrand(props.item, text === '' ? { action: 'solve' } : { text });
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}

// Ф65-1 (И3): ответ на открытый вопрос агента — тем же путём, что в «Мой агент» (`reply_to` = id вопроса).
async function answerAsk(): Promise<void> {
  const q = ask.value;
  const text = askText.value.trim();
  if (q === null || text === '') return;
  busy.value = true;
  error.value = null;
  try {
    const result = await store.answerAgentAsk(q.id, text);
    if (!result.ok) error.value = result.message;
    else askText.value = '';
  } finally {
    busy.value = false;
  }
}

async function cancelErrand(): Promise<void> {
  if (props.item.errand === null) return;
  busy.value = true;
  error.value = null;
  try {
    const result = await store.cancelErrand(props.item.errand.id);
    if (!result.ok) error.value = result.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="owner-card"
    :class="{ 'owner-card--urgent': urgent && !gone, 'owner-card--gone': gone }"
    :data-ref="item.ref"
    :data-kind="item.kind"
    :data-gone="gone ? '1' : undefined"
    @focusin="focused = true"
    @focusout="onFocusOut"
  >
    <p class="owner-path">
      {{ pathLine(item) }}<template v-if="item.machine_name"><span data-test="owner-machine"> · {{ item.machine_name }}</span></template>
    </p>
    <ErrandHands
      v-if="errand && item.errand"
      :errand="item.errand"
      test-prefix="owner"
    />
    <p class="owner-meta">
      <span class="owner-kind">{{ kindLabel(item.kind) }}</span>
      <span
        v-if="item.money"
        class="owner-mark owner-mark--money"
        data-test="owner-money"
      >💰 деньги</span>
      <span class="owner-age">{{ age }}</span>
      <span
        v-if="until"
        class="owner-until"
        :class="{ 'owner-until--urgent': urgent }"
      >{{ until }}</span>
      <span
        v-if="withAgent"
        class="owner-mark owner-mark--agent"
      >у агента</span>
      <span
        v-else-if="inChannelOnly"
        class="owner-mark"
      >в канале</span>
    </p>
    <p
      v-if="passed"
      class="owner-passed"
      data-test="owner-passed"
    >
      {{ passed }}
    </p>
    <p
      v-if="errand"
      class="owner-errand"
      data-test="owner-errand"
    >
      {{ errand }}
      <button
        type="button"
        class="link-button"
        data-test="owner-take-back"
        :disabled="busy"
        @click="cancelErrand"
      >
        Остановить агента
      </button>
    </p>
    <ErrandReturned
      v-else-if="returned"
      class="owner-returned"
      test-prefix="owner"
      :returned="returned"
      :busy="busy"
      @say="letDecide"
      @talk="openWrite({ kind: 'owner_channel', branch: item.session_id ?? null }, { ref: item.ref, title: item.brief || title })"
    />
    <!-- Ф65-1 (И3): агент спросил владельца по этой карточке — вопрос и поле ответа здесь же -->
    <div
      v-if="ask"
      class="owner-ask"
      data-test="owner-ask"
    >
      <p class="owner-ask-text">
        агент спрашивает: {{ ask.text }}
      </p>
      <textarea
        v-model="askText"
        rows="2"
        class="owner-text-input"
        aria-label="Ответ агенту"
        placeholder="ответ агенту"
        data-test="owner-ask-input"
        @keydown="onSubmitKey($event, askButton)"
      />
      <button
        ref="askButton"
        type="button"
        class="owner-answer owner-answer--main"
        data-test="owner-ask-send"
        :title="keyLabel"
        :disabled="busy || askText.trim() === ''"
        @click="answerAsk"
      >
        Ответить агенту
      </button>
    </div>

    <p
      v-if="item.brief && !isHuman && !hasQuestions && !isDraft"
      class="owner-brief"
    >
      {{ item.brief }}
    </p>

    <!-- человек: поля «где и что сделать» всегда на виду -->
    <template v-if="isHuman">
      <OwnerFullBody
        :item="item"
        :now-ms="nowMs"
        in-card
      />
      <button
        v-if="shot && humanId !== null"
        type="button"
        class="human-shot"
        aria-label="Открыть скриншот"
        @click="shotOpen = true"
      >
        <img
          :src="shotSrc"
          alt="скриншот"
        >
      </button>
    </template>

    <!-- вопрос: варианты и свой ответ на каждый -->
    <Teleport
      v-else-if="hasQuestions"
      :to="answerTo"
      :disabled="!answerReady"
    >
    <div class="work-questions">
      <fieldset
        v-for="q in questions"
        :key="q.n"
        class="work-question"
        :disabled="busy"
      >
        <legend>{{ q.shown }}</legend>
        <div class="work-question-options">
          <button
            v-for="opt in q.options"
            :key="opt"
            type="button"
            class="work-question-option"
            :class="{ 'work-question-option--picked': (picked[q.n] ?? []).includes(opt) }"
            :aria-pressed="(picked[q.n] ?? []).includes(opt)"
            @click="toggleOption(q.n, opt, q.multi)"
          >
            {{ opt }}
          </button>
        </div>
        <input
          v-model="custom[q.n]"
          type="text"
          class="work-question-custom"
          placeholder="свой ответ"
          aria-label="Свой ответ"
          @keydown="onSubmitKey($event, answerButton)"
        >
        <DictateButton />
      </fieldset>
    </div>
    </Teleport>

    <!-- черновик: куда уйдёт и правка текста (brief повторял бы и то и другое) -->
    <template v-else-if="isDraft">
      <OwnerFullBody
        :item="item"
        :now-ms="nowMs"
        in-card
      />
      <Teleport
        :to="answerTo"
        :disabled="!answerReady"
      >
        <textarea
          v-model="draftText"
          class="owner-draft-edit"
          rows="3"
          aria-label="Текст черновика"
        />
        <DictateButton />
      </Teleport>
    </template>

    <template v-else-if="foldable && more">
      <p class="work-request-views">
        <button
          v-if="short"
          type="button"
          class="link-button owner-expand"
          :aria-expanded="expanded"
          @click="expanded = !expanded"
        >
          {{ expanded ? 'свернуть' : 'развернуть' }}
        </button>
        <button
          v-else
          type="button"
          class="link-button owner-open"
          @click="modalOpen = true"
        >
          открыть окном
        </button>
      </p>
      <OwnerFullBody
        v-if="short && expanded"
        :item="item"
        :now-ms="nowMs"
        in-card
      />
    </template>
    <p
      v-if="(hasQuestions || isDraft) && !short"
      class="work-request-views"
    >
      <button
        type="button"
        class="link-button owner-open"
        @click="modalOpen = true"
      >
        открыть окном
      </button>
    </p>

    <Teleport
      :to="answerTo"
      :disabled="!answerReady"
    >
      <div class="owner-answer-block">
        <div
          v-if="notice"
          class="owner-gone"
          role="status"
          data-test="owner-gone"
        >
          <p class="owner-gone-text">
            {{ notice }}
          </p>
          <p class="owner-gone-actions">
            <button
              v-if="successor"
              type="button"
              class="owner-answer owner-answer--main"
              data-test="owner-gone-new"
              @click="toNew"
            >
              К новой
            </button>
            <button
              v-if="typedParts.length > 0"
              type="button"
              class="owner-answer"
              data-test="owner-gone-copy"
              @click="copyTyped"
            >
              {{ copied ? 'Скопировано' : 'Скопировать мой текст' }}
            </button>
            <button
              type="button"
              class="owner-answer"
              data-test="owner-gone-close"
              @click="closeGone"
            >
              Закрыть
            </button>
          </p>
        </div>
        <div class="owner-actions">
          <template
            v-for="a in item.answers"
            :key="a.action"
          >
            <button
              :ref="(el) => { if (a.needs_answers === true) answerButton = el as HTMLButtonElement | null; }"
              type="button"
              class="owner-answer"
              :class="{ 'owner-answer--main': a.needs_answers === true || a.action === 'approve' || a.action === 'allow' }"
              :data-action="a.action"
              :title="a.needs_answers === true ? keyLabel : undefined"
              :disabled="disabled(a)"
              @click="pressAnswer(a)"
            >
              {{ a.label }}
            </button>
          </template>
          <button
            v-if="isDraft && sendAt !== null"
            type="button"
            class="owner-answer"
            data-action="send_now"
            :disabled="busy || blocked"
            @click="sendNow"
          >
            Отправить сейчас
          </button>
          <input
            v-if="commentAnswers"
            v-model="comment"
            type="text"
            class="work-deny-comment"
            placeholder="комментарий (необязательно)"
            aria-label="Комментарий к ответу"
            maxlength="2000"
          >
          <DictateButton />
        </div>

        <div
          v-if="textFor"
          class="owner-text"
          :class="{ 'owner-text--drop': dragOver }"
          @dragover="onDragOver"
          @dragleave="dragOver = false"
          @drop="onDrop"
        >
          <textarea
            ref="textArea"
            v-model="textValue"
            rows="3"
            class="owner-text-input"
            :aria-label="textFor.label"
            :placeholder="textFor.mode === 'errand' ? 'что поручить агенту' : 'текст'"
            @keydown="onSubmitKey($event, sendButton)"
            @paste="onPaste"
          />
          <DictateButton />
          <SessionFileChips
            v-if="canAttach"
            :files="files"
            @remove="(localId) => item.session_id !== null && store.removeSessionFile(item.session_id, localId)"
          />
          <p class="owner-text-actions">
            <button
              ref="sendButton"
              type="button"
              class="owner-answer owner-answer--main"
              data-test="owner-text-send"
              :title="keyLabel"
              :disabled="busy || uploading || (textValue.trim() === '' && !(canAttach && readyIds.length > 0)) || (gone && (blocked || textFor.mode === 'errand'))"
              @click="submitText"
            >
              {{ textFor.label }}
            </button>
            <button
              v-if="canAttach"
              type="button"
              class="session-attach"
              data-test="owner-attach"
              title="Приложить файл"
              @click="fileInput?.click()"
            >
              📎 Файл
            </button>
            <input
              v-if="canAttach"
              ref="fileInput"
              type="file"
              multiple
              class="session-file-input"
              hidden
              @change="onPick"
            >
            <button
              type="button"
              class="link-button"
              @click="textFor = null"
            >
              Отмена
            </button>
          </p>
        </div>

        <p
          v-if="!showcase && !errand && !gone && item.agent_actions.length > 0"
          class="owner-agent-actions"
        >
          <span class="owner-agent-label">Поручить агенту:</span>
          <button
            v-for="act in item.agent_actions"
            :key="act.id"
            type="button"
            class="owner-agent-action"
            :data-agent-action="act.id"
            :disabled="busy"
            @click="errandAction(act.id, act.needs_text, act.label)"
          >
            {{ act.label }}
          </button>
        </p>

        <p
          v-if="error"
          class="work-error"
          role="alert"
        >
          {{ error }}
        </p>
      </div>
    </Teleport>
    <p class="owner-discuss">
      <button
        v-if="!showcase"
        type="button"
        class="link-button owner-discuss-link"
        data-test="owner-discuss"
        @click="openWrite({ kind: 'owner_channel', branch: item.session_id ?? null }, { ref: item.ref, title: item.brief || title })"
      >
        обсудить с агентом
      </button>
      <button
        v-if="item.session_id !== null"
        type="button"
        class="link-button owner-open-session"
        data-test="owner-open-session"
        @click="openSession"
      >
        открыть сессию
      </button>
    </p>
    <OwnerFullModal
      v-if="modalOpen"
      :title="title"
      :item="item"
      :answer-id="answerId"
      :can-open-session="item.session_id !== null"
      @close="modalOpen = false"
      @open-session="openSession"
    />
    <HumanShotModal
      v-if="shotOpen"
      :src="shotSrc"
      :title="item.brief"
      @close="shotOpen = false"
    />
  </div>
</template>
