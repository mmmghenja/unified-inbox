<script setup lang="ts">
// Ф35-3 (решения 15–16): диалог «Владелец <-> агент» — лента по возрастанию, поле ввода (Ctrl+Enter и кнопка; фокус после
// отправки остаётся в поле). Тексты — чужой ввод: только текстовая интерполяция, без markdown. Открытый диалог
// отмечает ответы агента прочитанными. Ошибка ядра — текстом под полем, введённое не теряется.
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';

import { ApiError } from '../api/client';
import { store } from '../store/instance';
import { dialogWith, statusLabel, type AgentLine } from '../store/peerMsgs';
import type { AgentDraft } from './agentDraft';
import { onSubmitKey, showSubmitHint, submitKeyLabel } from '../util/submitKeys';
import DictateButton from './DictateButton.vue';

// Ф36-2 B3: черновик и ошибка могут жить в родителе (`AgentsBlock`) — переживают перерисовку списка и исчезновение
// линии; без `draft` (диалог сам по себе) состояние локальное.
const props = defineProps<{ line: AgentLine; draft?: AgentDraft }>();

const state = props.draft ?? reactive<AgentDraft>({ text: '', error: null, busy: false });
const text = computed({ get: () => state.text, set: (v: string) => { state.text = v; } });
const busy = computed(() => state.busy);
const textArea = ref<HTMLTextAreaElement | null>(null);
const sendButton = ref<HTMLButtonElement | null>(null);
const keyLabel = submitKeyLabel();
const showHint = showSubmitHint();

const messages = computed(() => dialogWith(store.state.peerMsgs.items, props.line.key, props.line.sub?.id));
const offline = computed(() => props.line.sub === null);
// Безрольный (или вне проектов с безрольной подпиской) не в сети: адресата нет — писать нельзя.
const disabled = computed(() => offline.value && !props.line.hasRole);

const hint = computed(() => {
  if (!offline.value) return '';
  return props.line.hasRole
    ? 'Агент не в сети: получит сессия, которая подпишется на эту линию; ждёт двое суток.'
    : 'Агент не в сети — написать можно только пока агент в сети.';
});

function time(ts: number): string {
  return new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | null;
    const detail = body?.detail;
    if (detail !== null && typeof detail === 'object') {
      const inner = detail as { detail?: unknown; message?: unknown; error?: unknown };
      for (const v of [inner.detail, inner.message, inner.error]) if (typeof v === 'string' && v !== '') return v;
      return 'Слишком часто — подожди немного.';
    }
    if (err.message) return err.message;
  }
  return 'Не отправилось — проверь связь и повтори.';
}

async function send(): Promise<void> {
  const value = text.value.trim();
  if (busy.value || disabled.value || value === '') return;
  state.busy = true;
  state.error = null;
  try {
    await store.sendToAgent(value, props.line.address);
    // введённое могло смениться, пока шла отправка, — чистим только то, что ушло
    if (state.text.trim() === value) state.text = '';
  } catch (err) {
    state.error = errorText(err);
  } finally {
    state.busy = false;
    // Владелец 28.09: после отправки фокус остаётся в поле.
    void nextTick(() => textArea.value?.focus());
  }
}

function markRead(): void {
  void store.markAgentRepliesRead(props.line.key, props.line.sub?.id);
}

onMounted(markRead);
// Ответ пришёл, пока диалог открыт, — тоже прочитан.
watch(() => props.line.unread, (n) => {
  if (n > 0) markRead();
});
</script>

<template>
  <section
    class="agent-dialog"
    :aria-label="`Диалог с агентом ${props.line.name}`"
  >
    <ol class="agent-dialog-feed">
      <li
        v-for="msg in messages"
        :key="msg.id"
        class="agent-msg"
        :class="msg.from_user ? 'agent-msg--me' : 'agent-msg--agent'"
      >
        <span class="agent-msg-text">{{ msg.text }}</span>
        <span class="agent-msg-meta">
          {{ msg.from_user ? 'Владелец' : props.line.name }} · {{ time(msg.created_ts) }}
          <template v-if="msg.from_user"> · {{ statusLabel(msg) }}</template>
        </span>
      </li>
      <li
        v-if="messages.length === 0"
        class="agent-msg agent-msg--note"
      >
        Пока ничего не писали.
      </li>
    </ol>
    <form
      class="agent-dialog-input"
      @submit.prevent="send"
    >
      <textarea
        ref="textArea"
        v-model="text"
        class="agent-dialog-text"
        rows="2"
        maxlength="4000"
        :disabled="disabled"
        :placeholder="`Написать агенту ${props.line.name}`"
        :aria-label="`Написать агенту ${props.line.name}`"
        enterkeyhint="enter"
        @keydown="onSubmitKey($event, sendButton)"
      />
      <DictateButton />
      <div class="work-actions">
        <button
          ref="sendButton"
          type="submit"
          :title="keyLabel"
          :disabled="busy || disabled || text.trim() === ''"
        >
          Отправить
        </button>
        <span
          v-if="showHint"
          class="submit-hint"
          aria-hidden="true"
        >{{ keyLabel }}</span>
      </div>
      <p
        v-if="hint !== ''"
        class="agent-hint"
      >
        {{ hint }}
      </p>
      <p
        v-if="state.error !== null"
        class="agent-error"
        role="alert"
      >
        {{ state.error }}
      </p>
    </form>
  </section>
</template>
