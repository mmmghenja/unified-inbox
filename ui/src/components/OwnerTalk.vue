<script setup lang="ts">
// Ф58-1 B3: разговор владельца с его агентом — лента `/owner_channel/talk` (новые снизу, «ещё» — раньше), поле ввода, плашка
// «по карточке: …» (реплика уйдёт с `ref` карточки очереди). Тексты — чужой ввод: только интерполяция.
// Ф61-1: реплика владельца, пришедшая из бота Telegram (`via = 'bot'`), помечена «· из Telegram».
// Открытый разговор отмечает ответы агента прочитанными; снимок очереди с новыми `talk_unread`/`talk_open`
// заставляет перечитать ленту.
// 23.10: к реплике — файлы и картинки («📎», вставка из буфера, перетаскивание); в ленте картинка — миниатюрой,
// прочее — ссылкой на скачивание. Имена файлов — только текстом.
import { computed, nextTick, onMounted, ref, watch } from 'vue';

import { talkFileUrl } from '../api/client';
import type { OwnerItem } from '../api/types';
import { store } from '../store/instance';
import { writeModal, type WriteCardRef } from '../store/writeModal';
import {
  attachTalkFiles,
  branchTitle,
  loadBranches,
  loadOlderTalk,
  loadTalk,
  markTalkRead,
  removeTalkFile,
  selectBranch,
  sendTalk,
  sortedBranches,
  talkFiles,
  talkState,
  unreadTalk,
} from '../store/ownerTalk';
import { layout } from '../util/layout';
import { onSubmitKey, showSubmitHint, submitKeyLabel } from '../util/submitKeys';
import ModelText from './ModelText.vue';
import DictateButton from './DictateButton.vue';
import SessionFileChips from './SessionFileChips.vue';
import OwnerFullModal from './OwnerFullModal.vue';

// Ф66-2: слева ветки («Общее» и по сессиям), справа лента выбранной. `branch` — открыть ветку (id сессии, `null` —
// «Общее»); без него остаётся прежняя. Телефон: сначала список веток, тап — ветка, «‹» — назад.
const props = defineProps<{ card: WriteCardRef | null; branch?: number | null }>();
const emit = defineEmits<{ 'clear-card': [] }>();

const text = ref('');
const busy = ref(false);
const error = ref<string | null>(null);
const feed = ref<HTMLElement | null>(null);
const input = ref<HTMLTextAreaElement | null>(null);
const sendButton = ref<HTMLButtonElement | null>(null);
const keyLabel = submitKeyLabel();
const showHint = showSubmitHint();
const opened = ref<OwnerItem | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const dragOver = ref(false);
const uploading = computed(() => talkFiles.list.some((f) => f.status === 'uploading'));
const readyIds = computed(() => talkFiles.list.filter((f) => f.status === 'ready' && f.fileId !== null).map((f) => f.fileId!));

function attach(list: FileList | File[] | null | undefined): void {
  if (!list || list.length === 0) return;
  const note = attachTalkFiles(Array.from(list));
  error.value = note;
}
function onPick(event: Event): void {
  const el = event.target as HTMLInputElement;
  attach(el.files);
  el.value = '';
}
function onPaste(event: ClipboardEvent): void {
  const pasted = event.clipboardData?.files;
  if (!pasted || pasted.length === 0) return; // текст вставляется как обычно
  event.preventDefault();
  attach(pasted);
}
function onDragOver(event: DragEvent): void {
  if (!Array.from(event.dataTransfer?.types ?? []).includes('Files')) return;
  event.preventDefault();
  dragOver.value = true;
}
function onDrop(event: DragEvent): void {
  dragOver.value = false;
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  attach(event.dataTransfer.files);
}

const online = computed(() => store.state.owner_channel.agent.online);
const items = computed(() => talkState.items);
const phone = computed(() => layout.mode === 'phone');
const branches = computed(() => sortedBranches(talkState.branches));
const current = computed(() => talkState.branches.find((b) => b.session_id === talkState.branch) ?? null);
const currentTitle = computed(() => (current.value === null ? 'Общее' : branchTitle(current.value)));
// На телефоне список веток и лента — по очереди.
const listShown = ref(false);

async function pickBranch(id: number | null): Promise<void> {
  if (id !== talkState.branch) {
    if (props.card !== null) emit('clear-card');
    selectBranch(id);
  }
  listShown.value = false;
  await refresh();
}

function openBranchSession(): void {
  const id = talkState.branch;
  if (id !== null) writeModal.target = { kind: 'session', id };
}

function time(ts: number): string {
  return new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function queueItem(ref: string): OwnerItem | undefined {
  return store.state.owner_channel.items.find((i) => i.ref === ref);
}

function openCard(ref: string): void {
  const item = queueItem(ref);
  if (item !== undefined) opened.value = item;
}

async function toBottom(): Promise<void> {
  await nextTick();
  const el = feed.value;
  if (el !== null) el.scrollTop = el.scrollHeight;
}

/** Лента у низа (с запасом в 80 px) — новое показывается само; владелец листает выше — не сбрасывать его. */
function nearBottom(): boolean {
  const el = feed.value;
  return el === null || el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}

async function refresh(): Promise<void> {
  void loadBranches();
  // 23.10: строка хода агента обновляет ленту раз в 5 с — прокрутка вниз только если владелец и так внизу.
  const stick = !talkState.loaded || nearBottom();
  await loadTalk();
  if (stick) await toBottom();
  if (unreadTalk().length > 0) void markTalkRead();
}

async function send(): Promise<void> {
  const value = text.value.trim();
  const files = readyIds.value;
  if (busy.value || uploading.value || (value === '' && files.length === 0)) return;
  busy.value = true;
  error.value = null;
  try {
    const res = await sendTalk(value, props.card?.ref ?? null, files);
    if (res.ok) {
      if (text.value.trim() === value) text.value = '';
      emit('clear-card');
      void store.loadOwner();
      await toBottom();
    } else error.value = res.message;
  } finally {
    busy.value = false;
    void nextTick(() => input.value?.focus());
  }
}

onMounted(() => {
  if (props.branch !== undefined) selectBranch(props.branch);
  // Ф80-1 F2 (Т10): на телефоне окно открывается сразу на «Общее» — поле ввода видно; список веток — по «‹».
  else if (phone.value) selectBranch(null);
  listShown.value = false;
  void refresh();
});
watch(
  () => props.branch,
  (next) => {
    if (next === undefined) return;
    listShown.value = false;
    if (next !== talkState.branch) {
      selectBranch(next);
      void refresh();
    }
  },
);
watch(
  () =>
    [store.state.owner_channel.agent.talk_unread, store.state.owner_channel.agent.talk_open, store.state.owner_channel.agent.talk_progress] as const,
  () => void refresh(),
);
</script>

<template>
  <section
    class="owner-talk"
    aria-label="Разговор с агентом"
  >
    <nav
      v-if="!phone || listShown"
      class="owner-talk-branches"
      aria-label="Ветки разговора"
    >
      <button
        v-for="b in branches"
        :key="b.session_id === null ? 'general' : `s${b.session_id}`"
        type="button"
        class="board-row owner-branch"
        :class="{ 'owner-branch--active': b.session_id === talkState.branch }"
        :data-branch="b.session_id === null ? 'general' : String(b.session_id)"
        @click="pickBranch(b.session_id)"
      >
        <span class="board-row-name">{{ branchTitle(b) }}</span>
        <span
          v-if="b.unread > 0"
          class="badge badge-react"
          data-test="branch-unread"
        >{{ b.unread }}</span>
      </button>
    </nav>
    <div
      v-if="!phone || !listShown"
      class="owner-talk-main"
    >
      <header class="owner-talk-head">
        <button
          v-if="phone"
          type="button"
          class="back-button"
          aria-label="Назад к веткам"
          data-test="branch-back"
          @click="listShown = true"
        >
          ‹
        </button>
        <b
          class="owner-talk-title"
          data-test="branch-title"
        >{{ currentTitle }}</b>
        <button
          v-if="talkState.branch !== null"
          type="button"
          class="link-button"
          data-test="branch-open-session"
          @click="openBranchSession"
        >
          открыть сессию
        </button>
      </header>
    <p
      v-if="talkState.gone"
      class="board-empty"
      data-test="talk-gone"
    >
      Ядро ещё не умеет разговор с агентом — нужен деплой.
    </p>
    <ol
      v-else
      ref="feed"
      class="agent-dialog-feed owner-talk-feed"
    >
      <li
        v-if="!talkState.olderDone && items.length > 0"
        class="agent-msg agent-msg--note"
      >
        <button
          type="button"
          class="link-button"
          data-test="talk-older"
          @click="loadOlderTalk"
        >
          ещё
        </button>
      </li>
      <li
        v-for="msg in items"
        :key="msg.id"
        class="agent-msg"
        :class="msg.author === 'owner_channel' ? 'agent-msg--me' : 'agent-msg--agent'"
        :data-talk-id="msg.id"
      >
        <!-- Ф56-5б: ответ агента — текст модели (markdown, у блоков кода «▶ в терминал»); реплика владельца — текстом. -->
        <div
          v-if="msg.author === 'agent'"
          class="agent-msg-text agent-msg-text--md"
        >
          <ModelText
            :text="msg.text"
            :session-id="talkState.branch"
            :project-id="current?.project_id"
            runnable
          />
        </div>
        <span
          v-else-if="msg.text !== ''"
          class="agent-msg-text"
        >{{ msg.text }}</span>
        <ul
          v-if="msg.files && msg.files.length > 0"
          class="talk-files"
          data-test="talk-files"
        >
          <li
            v-for="f in msg.files"
            :key="f.id"
            class="talk-file"
          >
            <span
              v-if="f.gone"
              class="talk-file-gone"
            >{{ f.name }} · стёрт</span>
            <a
              v-else-if="f.kind === 'image'"
              :href="talkFileUrl(f.id)"
              target="_blank"
              rel="noopener"
              :title="f.name"
            >
              <img
                class="talk-file-img"
                :src="talkFileUrl(f.id)"
                :alt="f.name"
                loading="lazy"
              >
            </a>
            <a
              v-else
              :href="talkFileUrl(f.id)"
              :download="f.name"
            >📎 {{ f.name }}</a>
          </li>
        </ul>
        <!-- 23.10: ход агента по этой реплике — план, шаги, время; уходит, когда реплика обработана. -->
        <pre
          v-if="msg.author === 'owner_channel' && msg.handled_ts === null && msg.progress"
          class="talk-progress"
          data-test="talk-progress"
        >{{ msg.progress }}</pre>
        <span class="agent-msg-meta">
          {{ msg.author === 'owner_channel' ? 'Владелец' : 'агент' }} · {{ time(msg.ts) }}
          <template v-if="msg.author === 'owner_channel' && msg.via === 'bot'">
            · <span data-test="talk-via-bot">из Telegram</span>
          </template>
          <template v-if="msg.author === 'owner_channel' && msg.handled_ts === null">
            · <span data-test="talk-waits">{{ msg.progress ? 'агент работает' : 'ждёт агента' }}</span>
          </template>
          <template v-if="msg.ref !== null">
            ·
            <button
              v-if="queueItem(msg.ref) !== undefined"
              type="button"
              class="link-button"
              data-test="talk-ref"
              @click="openCard(msg.ref)"
            >
              карточка
            </button>
            <span v-else>карточка закрыта</span>
          </template>
        </span>
      </li>
      <li
        v-if="items.length === 0 && talkState.loaded"
        class="agent-msg agent-msg--note"
      >
        Пока ничего не писали.
      </li>
    </ol>
    <form
      class="agent-dialog-input"
      :class="{ 'owner-text--drop': dragOver }"
      @submit.prevent="send"
      @dragover="onDragOver"
      @dragleave="dragOver = false"
      @drop="onDrop"
    >
      <p
        v-if="card !== null"
        class="owner-talk-ref"
        data-test="talk-card"
      >
        по карточке: {{ card.title }}
        <button
          type="button"
          class="link-button"
          aria-label="Убрать ссылку на карточку"
          @click="emit('clear-card')"
        >
          ×
        </button>
      </p>
      <textarea
        ref="input"
        v-model="text"
        class="agent-dialog-text"
        rows="2"
        maxlength="8000"
        placeholder="Написать агенту"
        aria-label="Написать агенту"
        enterkeyhint="enter"
        @keydown="onSubmitKey($event, sendButton)"
        @paste="onPaste"
      />
      <DictateButton />
      <SessionFileChips
        :files="talkFiles.list"
        no-note
        @remove="removeTalkFile"
      />
      <div class="work-actions">
        <button
          ref="sendButton"
          type="submit"
          :title="keyLabel"
          :disabled="busy || uploading || (text.trim() === '' && readyIds.length === 0) || talkState.gone"
        >
          Отправить
        </button>
        <button
          type="button"
          class="session-attach"
          data-test="talk-attach"
          title="Приложить файл или картинку"
          @click="fileInput?.click()"
        >
          📎 Файл
        </button>
        <input
          ref="fileInput"
          type="file"
          multiple
          class="session-file-input"
          data-test="talk-file-input"
          hidden
          @change="onPick"
        >
        <span
          v-if="showHint"
          class="submit-hint"
          aria-hidden="true"
        >{{ keyLabel }}</span>
      </div>
      <p
        v-if="!online"
        class="agent-hint"
        data-test="talk-offline"
      >
        агент не на связи — сообщение дождётся
      </p>
      <p
        v-if="error !== null"
        class="agent-error"
        role="alert"
      >
        {{ error }}
      </p>
    </form>
    </div>
    <OwnerFullModal
      v-if="opened !== null"
      :title="opened.brief"
      :item="opened"
      @close="opened = null"
    />
  </section>
</template>
