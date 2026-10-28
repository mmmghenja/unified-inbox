<script setup lang="ts">
// Поле ввода очищается ТОЛЬКО после успешного ответа `send`; при ошибке текст остаётся, ошибка показана.
// Повтор при сетевой ошибке (в т.ч. ручной повторный клик по «Отправить» для того же черновика) — с ТЕМ ЖЕ
// `idempotency_key` (спека). Оптимистичная строка не рисуется — строка появится через /sync (store.send уже
// форсирует его после успешного POST).
//
// Спека 2e, раздел «Стор»/«Композер»: `thread`/`replyTo` — необязательные пропсы. Черновик и
// `idempotency_key` — локальное состояние ЭТОГО экземпляра компонента: лента и панель треда монтируют СВОИ
// отдельные `<Composer>` (сиблинги, не один общий), поэтому набранное в одном не пропадает при работе с
// другим — компоненты просто не делят состояние.
//
// Ф10-4 C (docs/architecture.md): файлы — скрепкой, вставкой из буфера и перетаскиванием,
// только в беседы Telegram и Slack (у почты кнопки нет). Прикреплённое сразу грузится в ядро (`POST /uploads`);
// превью картинки — `data:` (CSP не пускает `blob:`). Отправка — по одному сообщению на файл по порядку, у
// каждого свой ключ идемпотентности (П12), текст — подписью к первому; после первой успешной отправки текст из
// поля убирается (повтор не шлёт подпись дважды). Ошибка — строка под полем, неотправленное остаётся.
//
// Ф11-3b C (docs/architecture.md): скрепка и в беседах почты (вложения в ответе на письмо), но
// не в беседе-заготовке нового адресата. Отправка — пачками (`splitFileBatches`): одна пачка = один `send` с
// `files: [id…]` = одно сообщение источнику (альбом Telegram, одно сообщение Slack, одно письмо). Подпись и
// ответ — с первой пачкой, у каждой пачки свой ключ идемпотентности (живёт до её успеха, сброс — при правке
// текста). Письмо не помещается в 17 МиБ — ошибка под полем до отправки.
//
// Ф12-5 C3 (docs/architecture.md): поле берёт фокус, когда беседу только что создали из
// «Новое сообщение» (`composerFocusChannelId`). П7: отказ ядра 409/422 — его текстом.
// Ф14-2 D4 (docs/architecture.md): скрепка в беседе-заготовке — у всех источников с файлами;
// файлы в заготовку — одной пачкой (одно первое сообщение), больше 10 — подсказка до отправки.
// Ф14-4 D1/D2 (docs/architecture.md): рядом со скрепкой — микрофон (`VoiceRecorder`, только в
// безопасном контексте с `MediaRecorder`); иначе (телефон через VPN по http) — «Записать»: системный
// диктофон/выбор аудиофайла, файл идёт обычным вложением (`addFiles`), без признака голосового.
// Ф14-9 П4 (docs/architecture.md): на телефоне по https рядом со скрепкой — «Из буфера»
// (`navigator.clipboard.read()` -> картинки -> `addFiles`): отдаёт ли мобильный браузер картинку в `paste`, не
// проверено. Пусто или отказ в разрешении — строка «в буфере нет картинки».
// Ф27-1 C (docs/architecture.md): в беседе Slack `@` + 1–30 символов после пробела/начала строки —
// подсказка людей воркспейса (`GET /accounts/{id}/mention_people`, запрос с задержкой 200 мс, ответы кэшируются на
// беседу). Выбор вставляет в поле `@Имя ` и запоминает пару «метка -> id» (`util/mentions.ts`); при отправке метки
// целым словом уходят как `<@id>`. Не ушло — в поле возвращается текст с метками, пара — тоже.
import { computed, nextTick, onMounted, ref, watch } from 'vue';

import * as api from '../api/client';
import { ApiError } from '../api/client';
import type { MentionPerson } from '../api/types';
import { store } from '../store/instance';
import { canPasteFromClipboard, CLIPBOARD_NO_IMAGE, clipboardImages } from '../util/clipboard';
import { FIRST_MESSAGE_TOO_MANY, splitFileBatches } from '../util/fileBatches';
import { newId } from '../util/ids';
import {
  findMentionQuery,
  insertMention,
  type MentionMap,
  mentionLabel,
  pruneMap,
  toWire,
} from '../util/mentions';
import { restoreUnsent } from '../util/sendField';
import { usePopup } from '../util/popup';
import { onSubmitKey, submitKeyLabel } from '../util/submitKeys';
import { canRecordVoice } from '../util/voice';
import VoiceRecorder from './VoiceRecorder.vue';
import DictateButton from './DictateButton.vue';

const props = defineProps<{
  channelId: number;
  thread?: string | null;
  replyTo?: { extId: string; preview: string } | null;
}>();

const emit = defineEmits<{ 'cancel-reply': [] }>();

/** Источники, куда ядро принимает файлы (`uploads.SOURCES`); `fake` — источник стенда, `mail` — Ф11-3b C1. */
const FILE_SOURCES = new Set(['telegram', 'slack', 'mattermost', 'fake', 'mail', 'whatsapp']);
const MAX_FILES = 10;
/** Превью читается в память целиком (`data:`) — только для картинок разумного размера. */
const PREVIEW_MAX_BYTES = 10 * 1024 * 1024;

interface Attached {
  localId: number;
  file: File;
  name: string;
  size: number;
  preview: string | null;
  status: 'uploading' | 'ready' | 'error';
  uploadId: string | null;
  /** Вид из ответа `POST /uploads` (`photo`/`document`) — Telegram не смешивает их в одном альбоме. */
  kind: string;
  error: string | null;
}

const text = ref('');
const textArea = ref<HTMLTextAreaElement | null>(null);
const sendButton = ref<HTMLButtonElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const captureInput = ref<HTMLInputElement | null>(null);
const recorder = ref<{ start: () => Promise<void> } | null>(null);
// Ф90-3: 📎 — меню (файл, «Из буфера», «Записать голосовое»); поле растёт от 1 до 6 строк.
const attachMenuOpen = ref(false);
const attachButton = ref<HTMLElement | null>(null);
const attachMenu = ref<HTMLElement | null>(null);
usePopup({ open: attachMenuOpen, button: () => attachButton.value, panel: () => attachMenu.value });
const TEXT_MAX_PX = 144;

function grow(): void {
  const el = textArea.value;
  if (el === null) return;
  el.style.height = 'auto';
  if (el.scrollHeight > 0) el.style.height = `${Math.min(el.scrollHeight, TEXT_MAX_PX)}px`;
}
watch(text, () => void nextTick(grow), { flush: 'post' });
onMounted(grow);

function pickFile(): void {
  attachMenuOpen.value = false;
  openPicker();
}

function pasteFromMenu(): void {
  attachMenuOpen.value = false;
  void pasteFromClipboard();
}

function recordFromMenu(): void {
  attachMenuOpen.value = false;
  void recorder.value?.start();
}

function captureFromMenu(): void {
  attachMenuOpen.value = false;
  openCapture();
}
/** Ф14-4 решение 1: микрофон — в безопасном контексте с `MediaRecorder`; иначе «Записать» (решение 2). */
const micMode = canRecordVoice();
const clipboardMode = canPasteFromClipboard(); // Ф14-9 П4
const keyLabel = submitKeyLabel();
const sending = ref(false);
// Отправка с файлами идёт: подпись читается по пачкам — поле только для чтения (не `disabled`: фокус не теряется).
const sendingFiles = ref(false);
const error = ref<string | null>(null);
const attached = ref<Attached[]>([]);
// Приёмка Ф10-4 (ревью): загрузка привязана к беседе (П1) — при переходе в другую беседу прикреплённое не едет
// следом (ядро ответило бы 422); неотправленные загрузки ядро уберёт само через 24 ч.
watch(
  () => props.channelId,
  () => {
    attached.value = [];
    batchKeys.clear();
    error.value = null;
    mentionMap.value = {};
    closeSuggest();
    dismissedStart = null;
  },
);
const dragOver = ref(false);

// --- Ф27-1 C: подсказка `@` (Slack; Ф40-4 — и Mattermost: там выбор вставляет `@ник` как есть) -----------------------------------------------------------------------
const MENTION_DELAY_MS = 200;
const mentionMap = ref<MentionMap>({});
const suggestions = ref<MentionPerson[]>([]);
const suggestIndex = ref(0);
const suggestQuery = ref<{ start: number; query: string } | null>(null);
const suggestOpen = computed(() => suggestQuery.value !== null && suggestions.value.length > 0);
const accountIds = new Map<number, number>();
const suggestCache = new Map<string, MentionPerson[]>();
let suggestTimer: ReturnType<typeof setTimeout> | null = null;
let suggestSeq = 0;
let dismissedStart: number | null = null;

function closeSuggest(): void {
  if (suggestTimer !== null) clearTimeout(suggestTimer);
  suggestTimer = null;
  suggestSeq += 1;
  suggestQuery.value = null;
  suggestions.value = [];
  suggestIndex.value = 0;
}

async function loadSuggest(channelId: number, query: string, seq: number): Promise<void> {
  try {
    let accountId = accountIds.get(channelId);
    if (accountId === undefined) {
      accountId = await api.getChannelAccountId(channelId);
      accountIds.set(channelId, accountId);
    }
    const key = `${channelId}|${query.toLowerCase()}`;
    let people = suggestCache.get(key);
    if (people === undefined) {
      people = await api.searchMentionPeople(accountId, query, channelId);
      // Mattermost: упоминание — `@username`; человек без ника упомянуть нельзя — в подсказке его нет.
      if (channelSource.value === 'mattermost') people = people.filter((p) => nonEmpty(p.handle));
      suggestCache.set(key, people);
    }
    if (seq !== suggestSeq || channelId !== props.channelId) return; // запрос устарел
    suggestions.value = people;
    suggestIndex.value = 0;
  } catch {
    if (seq === suggestSeq) suggestions.value = []; // подсказка — удобство: сбой не мешает набирать
  }
}

function refreshSuggest(): void {
  if (channelSource.value !== 'slack' && channelSource.value !== 'mattermost') return;
  const area = textArea.value;
  const found = area ? findMentionQuery(text.value, area.selectionStart ?? text.value.length) : null;
  if (found === null) {
    dismissedStart = null;
    if (suggestQuery.value !== null) closeSuggest();
    return;
  }
  if (found.start === dismissedStart) return; // Esc — до следующего `@`
  const prev = suggestQuery.value;
  if (prev !== null && prev.start === found.start && prev.query === found.query) return;
  suggestQuery.value = found;
  if (suggestTimer !== null) clearTimeout(suggestTimer);
  suggestSeq += 1;
  const seq = suggestSeq;
  const cached = suggestCache.get(`${props.channelId}|${found.query.toLowerCase()}`);
  if (cached !== undefined && accountIds.has(props.channelId)) {
    suggestions.value = cached;
    suggestIndex.value = 0;
    return;
  }
  suggestTimer = setTimeout(() => void loadSuggest(props.channelId, found.query, seq), MENTION_DELAY_MS);
}

function nonEmpty(value: string | null): boolean {
  return value !== null && value.replace(/^@/, '').trim() !== '';
}

function pickSuggestion(person: MentionPerson): void {
  const q = suggestQuery.value;
  const area = textArea.value;
  if (q === null || area === null) return;
  const caret = area.selectionStart ?? text.value.length;
  // Mattermost: в текст — `@ник` как есть, пары «метка -> id» нет (`handle` ядра — без `@`). Slack — метка и пара.
  const isMm = channelSource.value === 'mattermost';
  const label = isMm ? `@${(person.handle ?? '').replace(/^@/, '')}` : mentionLabel(person, suggestions.value, mentionMap.value);
  if (!isMm) mentionMap.value = { ...mentionMap.value, [label]: person.id };
  const next = insertMention(text.value, q.start, caret, label);
  text.value = next.text;
  closeSuggest();
  onInput();
  void nextTick(() => {
    area.focus();
    area.setSelectionRange(next.caret, next.caret);
  });
}

function onComposerKeydown(event: KeyboardEvent): void {
  if (suggestOpen.value && !event.isComposing) {
    const n = suggestions.value.length;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      suggestIndex.value = (suggestIndex.value + (event.key === 'ArrowDown' ? 1 : n - 1)) % n;
      return;
    }
    const plainEnter = event.key === 'Enter' && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey;
    if (plainEnter || event.key === 'Tab') {
      event.preventDefault();
      const person = suggestions.value[suggestIndex.value];
      if (person) pickSuggestion(person);
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      dismissedStart = suggestQuery.value?.start ?? null;
      closeSuggest();
      return;
    }
  }
  onSubmitKey(event, sendButton.value);
}

function onComposerKeyup(event: KeyboardEvent): void {
  if (['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(event.key)) return;
  refreshSuggest();
}

let idempotencyKey: string | null = null;
/** Ф11-3b C2: ключ идемпотентности пачки — по составу (`localId` через запятую); живёт до успеха пачки. */
const batchKeys = new Map<string, string>();
let nextLocalId = 1;

const channel = computed(() => store.state.sync.channelsById[props.channelId]);
const channelSource = computed(() => channel.value?.source ?? '');
/** Ф12-1: беседа-заготовка нового адресата. Ф14-2 D4: файлы в неё — у всех источников, одной пачкой. */
const isNewRecipientDraft = computed(
  () => channel.value?.new_to != null || (channel.value?.ext_id ?? '').startsWith('new:'),
);
const canAttach = computed(() => FILE_SOURCES.has(channelSource.value));

// Ф12-5 C2: беседу только что создали из «Новое сообщение» — фокус в поле ввода (флаг сбрасывается здесь).
watch(
  () => store.state.composerFocusChannelId,
  (target) => {
    if (target === null || target !== props.channelId) return;
    store.state.composerFocusChannelId = null;
    void nextTick(() => textArea.value?.focus());
  },
  { immediate: true },
);
const uploading = computed(() => attached.value.some((a) => a.status === 'uploading'));
const hasFailed = computed(() => attached.value.some((a) => a.status === 'error'));
const canSend = computed(
  () =>
    !sending.value &&
    !uploading.value &&
    !hasFailed.value &&
    (text.value.trim() !== '' || attached.value.length > 0),
);

function uploadErrorText(err: unknown): string {
  if (err instanceof ApiError && err.status === 413) {
    return channelSource.value === 'mail' ? 'письмо: файл больше 17 МиБ' : 'файл больше 50 МиБ';
  }
  if (err instanceof ApiError && err.status === 507) return 'мало места на диске';
  return 'не загрузился';
}

function readPreview(item: Attached): void {
  if (!item.file.type.startsWith('image/') || item.size > PREVIEW_MAX_BYTES) return;
  const reader = new FileReader();
  reader.onload = () => {
    const found = attached.value.find((a) => a.localId === item.localId);
    if (found && typeof reader.result === 'string' && reader.result.startsWith('data:image/')) {
      found.preview = reader.result;
    }
  };
  reader.readAsDataURL(item.file);
}

async function upload(localId: number): Promise<void> {
  const item = attached.value.find((a) => a.localId === localId);
  if (!item) return;
  try {
    const result = await store.uploadFile(props.channelId, item.file, item.name);
    const found = attached.value.find((a) => a.localId === localId);
    if (!found) return; // убрали крестиком, пока грузилось — строку загрузки уберёт очистка ядра
    found.uploadId = result.upload_id;
    found.name = result.name;
    found.size = result.size;
    found.kind = result.kind;
    found.status = 'ready';
  } catch (err) {
    const found = attached.value.find((a) => a.localId === localId);
    if (!found) return;
    found.status = 'error';
    found.error = uploadErrorText(err);
  }
}

function addFiles(files: FileList | File[] | null | undefined): void {
  if (!canAttach.value || !files) return;
  error.value = null;
  for (const file of Array.from(files)) {
    if (attached.value.length >= MAX_FILES) {
      error.value = isNewRecipientDraft.value ? FIRST_MESSAGE_TOO_MANY : `Не больше ${MAX_FILES} файлов за раз.`;
      break;
    }
    const item: Attached = {
      localId: nextLocalId++,
      file,
      name: file.name || 'file',
      size: file.size,
      preview: null,
      status: 'uploading',
      uploadId: null,
      kind: 'document',
      error: null,
    };
    attached.value.push(item);
    readPreview(item);
    void upload(item.localId);
  }
}

function removeAttached(localId: number): void {
  attached.value = attached.value.filter((a) => a.localId !== localId);
  error.value = null;
}

function openPicker(): void {
  fileInput.value?.click();
}

function openCapture(): void {
  captureInput.value?.click();
}

function onPick(event: Event): void {
  const input = event.target as HTMLInputElement;
  addFiles(input.files);
  input.value = '';
}

async function pasteFromClipboard(): Promise<void> {
  let files: File[] = [];
  try {
    files = await clipboardImages();
  } catch {
    files = []; // отказ в разрешении — то же, что пусто
  }
  if (files.length === 0) {
    error.value = CLIPBOARD_NO_IMAGE;
    return;
  }
  addFiles(files);
}

function onPaste(event: ClipboardEvent): void {
  const files = event.clipboardData?.files;
  if (!canAttach.value || !files || files.length === 0) return;
  event.preventDefault();
  addFiles(files);
}

function hasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files');
}

function onDragOver(event: DragEvent): void {
  if (!canAttach.value || !hasFiles(event)) return;
  event.preventDefault();
  dragOver.value = true;
}

function onDragLeave(): void {
  dragOver.value = false;
}

function onDrop(event: DragEvent): void {
  dragOver.value = false;
  if (!canAttach.value || !event.dataTransfer?.files?.length) return;
  event.preventDefault();
  addFiles(event.dataTransfer.files);
}

function formatSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} МиБ`;
  if (size >= 1024) return `${Math.round(size / 1024)} КиБ`;
  return `${size} Б`;
}

async function sendText(value: string): Promise<void> {
  idempotencyKey ??= newId();
  await store.send(props.channelId, value, idempotencyKey, {
    thread: props.thread ?? undefined,
    replyTo: props.replyTo?.extId,
  });
  idempotencyKey = null;
}

function batchKey(batch: Attached[]): string {
  const id = batch.map((a) => a.localId).join(',');
  let key = batchKeys.get(id);
  if (key === undefined) {
    key = newId();
    batchKeys.set(id, key);
  }
  return key;
}

async function sendFiles(batches: Attached[][]): Promise<void> {
  // По порядку, одно сообщение на пачку. Подпись — только у первой отправляемой, пока текст ещё в поле; после
  // её успеха поле очищается, и следующие уходят без подписи (и повтор после сбоя — тоже).
  let first = true;
  for (const batch of batches) {
    const caption = text.value.trim();
    await store.send(props.channelId, toWire(caption, mentionMap.value), batchKey(batch), {
      thread: props.thread ?? undefined,
      replyTo: first ? props.replyTo?.extId : undefined,
      files: batch.map((a) => a.uploadId!),
    });
    const sent = new Set(batch.map((a) => a.localId));
    attached.value = attached.value.filter((a) => !sent.has(a.localId));
    batchKeys.delete(batch.map((a) => a.localId).join(','));
    if (caption !== '') {
      text.value = '';
      mentionMap.value = {};
    }
    idempotencyKey = null;
    if (first && props.replyTo) emit('cancel-reply');
    first = false;
  }
}

async function send(): Promise<void> {
  if (!canSend.value) return;
  const value = text.value.trim(); // с метками `@Имя` — его же возвращаем в поле, если не ушло
  const mapSnapshot = { ...mentionMap.value };
  closeSuggest();
  error.value = null;
  let batches: Attached[][] = [];
  if (attached.value.length > 0) {
    const split = splitFileBatches(attached.value, channelSource.value, { single: isNewRecipientDraft.value });
    if ('error' in split) {
      error.value = split.error; // письмо не помещается — до отправки, без запроса
      return;
    }
    batches = split.batches;
  }
  // Владелец 28.09: после Ctrl+Enter фокус остаётся в поле и можно сразу набирать следующее. Поле больше не
  // `disabled` на время отправки (отключённое поле теряет фокус): текст уходит — поле очищается СРАЗУ, не
  // ушёл — отправленное возвращается (`restoreUnsent`). С файлами подпись читается по пачкам — поле на это
  // время только `readonly` (фокус тоже не теряет).
  const refocus = document.activeElement === textArea.value || document.activeElement === sendButton.value;
  sending.value = true;
  const withFiles = attached.value.length > 0;
  sendingFiles.value = withFiles;
  try {
    if (!withFiles) {
      text.value = '';
      mentionMap.value = {};
      try {
        await sendText(toWire(value, mapSnapshot));
      } catch (err) {
        text.value = restoreUnsent(text.value, value);
        mentionMap.value = { ...mapSnapshot, ...mentionMap.value };
        throw err;
      }
      // Ответ отправлен — плашка «Ответ …» сделала своё дело, снимаем режим (как Telegram/большинство
      // мессенджеров: следующее сообщение по умолчанию уже не ответ).
      if (props.replyTo) emit('cancel-reply');
    } else {
      await sendFiles(batches);
    }
  } catch (err) {
    error.value = sendErrorText(err);
  } finally {
    sending.value = false;
    sendingFiles.value = false;
    if (refocus) void nextTick(() => textArea.value?.focus());
  }
}

/** Ф12-5 П7: отказ ядра (409 — «сначала уйдёт первое сообщение», 422 — файлы, лимиты) — его текстом; сеть и
 * прочее — как раньше. */
function sendErrorText(err: unknown): string {
  if (err instanceof ApiError && (err.status === 409 || err.status === 422) && err.message) {
    return `Не удалось отправить: ${err.message}`;
  }
  return 'Не удалось отправить. Проверьте соединение и попробуйте ещё раз.';
}

function onInput(): void {
  // Текст поменяли вручную поверх неотправленного черновика — это уже другое сообщение, не повтор. У пачек
  // файлов — тоже новые ключи: подпись к первой другая (если прошлая попытка всё же дошла, ядро ответит 422 —
  // загрузки уже использованы, — дубля не будет).
  idempotencyKey = null;
  batchKeys.clear();
  error.value = null;
  if (Object.keys(mentionMap.value).length > 0) mentionMap.value = pruneMap(text.value, mentionMap.value);
}

function onFieldInput(): void {
  onInput();
  refreshSuggest();
}

function cancelReply(): void {
  emit('cancel-reply');
}
</script>

<template>
  <form
    class="composer"
    :class="{ 'composer-drag': dragOver }"
    @submit.prevent="send"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <div
      v-if="replyTo"
      class="composer-reply-banner"
    >
      <span class="composer-reply-text">Ответ {{ replyTo.preview }}</span>
      <button
        type="button"
        class="composer-reply-cancel"
        aria-label="Отменить ответ"
        @click="cancelReply"
      >
        ×
      </button>
    </div>
    <ul
      v-if="attached.length > 0"
      class="composer-files"
      aria-label="Прикреплённые файлы"
    >
      <li
        v-for="item in attached"
        :key="item.localId"
        class="composer-file"
        :class="`composer-file-${item.status}`"
      >
        <img
          v-if="item.preview"
          class="composer-file-preview"
          :src="item.preview"
          alt=""
        >
        <span
          v-else
          class="composer-file-icon"
          aria-hidden="true"
        >📄</span>
        <span class="composer-file-name">{{ item.name }}</span>
        <span class="composer-file-state">
          <template v-if="item.status === 'uploading'">загружается…</template>
          <template v-else-if="item.status === 'error'">{{ item.error }}</template>
          <template v-else>{{ formatSize(item.size) }}</template>
        </span>
        <button
          type="button"
          class="composer-file-remove"
          :aria-label="`Убрать ${item.name}`"
          :disabled="sending"
          @click="removeAttached(item.localId)"
        >
          ×
        </button>
      </li>
    </ul>
    <input
      v-if="canAttach"
      ref="fileInput"
      type="file"
      class="composer-file-input"
      multiple
      hidden
      @change="onPick"
    >
    <input
      v-if="canAttach && !micMode"
      ref="captureInput"
      type="file"
      class="composer-capture-input"
      accept="audio/*"
      capture
      hidden
      @change="onPick"
    >
    <VoiceRecorder
      v-if="canAttach && micMode"
      ref="recorder"
      class="composer-voice"
      :channel-id="channelId"
      :thread="thread ?? null"
      :disabled="sending"
    />
    <div class="composer-row">
      <span
        v-if="canAttach"
        class="composer-attach-wrap"
      >
        <button
          ref="attachButton"
          type="button"
          class="composer-attach"
          aria-label="Прикрепить"
          title="Прикрепить: файл, картинка из буфера, голосовое"
          :aria-expanded="attachMenuOpen"
          :disabled="sending"
          @click="attachMenuOpen = !attachMenuOpen"
        >
          📎
        </button>
        <div
          v-if="attachMenuOpen"
          ref="attachMenu"
          class="composer-attach-menu"
          role="menu"
        >
          <button
            type="button"
            role="menuitem"
            class="composer-attach-file"
            @click="pickFile"
          >
            Файл…
          </button>
          <button
            v-if="clipboardMode"
            type="button"
            role="menuitem"
            class="composer-paste"
            title="Вставить картинку из буфера обмена"
            @click="pasteFromMenu"
          >
            Из буфера
          </button>
          <button
            v-if="micMode"
            type="button"
            role="menuitem"
            class="voice-start"
            @click="recordFromMenu"
          >
            Записать голосовое
          </button>
          <button
            v-else
            type="button"
            role="menuitem"
            class="composer-record"
            title="Записать голосовое аудиофайлом"
            @click="captureFromMenu"
          >
            Записать
          </button>
        </div>
      </span>
      <textarea
        ref="textArea"
        v-model="text"
        rows="1"
        placeholder="Сообщение…"
        enterkeyhint="enter"
        :readonly="sendingFiles"
        @input="onFieldInput"
        @click="refreshSuggest"
        @keyup="onComposerKeyup"
        @blur="closeSuggest"
        @paste="onPaste"
        @keydown="onComposerKeydown"
      />
      <DictateButton />
      <button
        ref="sendButton"
        type="submit"
        class="composer-send"
        :title="`Отправить (${keyLabel})`"
        aria-label="Отправить"
        :disabled="!canSend"
      >
        ➤
      </button>
    </div>
    <ul
      v-if="suggestOpen"
      class="mention-suggest"
      role="listbox"
      :aria-label="channelSource === 'mattermost' ? 'Люди Mattermost' : 'Люди Slack'"
    >
      <li
        v-for="(person, i) in suggestions"
        :key="person.id"
        class="mention-suggest-item"
        :class="{ 'mention-suggest-item--active': i === suggestIndex }"
        role="option"
        :aria-selected="i === suggestIndex"
        @mousedown.prevent
        @click="pickSuggestion(person)"
      >
        <span class="mention-suggest-name">{{ person.name }}</span>
        <span
          v-if="person.handle"
          class="mention-suggest-handle"
        >{{ person.handle }}</span>
      </li>
    </ul>
    <p
      v-if="error"
      class="composer-error"
      role="alert"
    >
      {{ error }}
    </p>
    <p
      v-else-if="hasFailed"
      class="composer-error"
      role="alert"
    >
      Файл не загрузился — уберите его, чтобы отправить остальное.
    </p>
  </form>
</template>
