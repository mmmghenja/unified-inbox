<script setup lang="ts">
// Ф62-6 (раздел G): «Ссылка-приглашение» в шапке беседы-группы WhatsApp — окно поверх. Открытие просит ссылку
// (`POST /group-ops/ui kind=link`, без карточки), окно ждёт исход заявки и показывает поле со ссылкой и
// «Копировать» (`util/copy.ts`); «Сбросить» (с подтверждением) выдаёт новую, старая перестаёт работать.
// Нужны права админа группы — иначе причина текстом. Закрывается по ✕, Esc, клику мимо.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { ApiError } from '../api/client';
import type { GroupOp } from '../api/types';
import { store } from '../store/instance';
import { copyText } from '../util/copy';
import { groupOpErrorText } from '../util/groupOps';

const props = defineProps<{ channelId: number; title: string }>();
const emit = defineEmits<{ close: [] }>();

const POLL_MS = 2000;

const dialog = ref<HTMLElement | null>(null);
const opId = ref<number | null>(null);
const link = ref<string | null>(null);
const error = ref<string | null>(null);
const copied = ref<boolean | null>(null);
const busy = ref(false);

const op = computed<GroupOp | null>(() => (opId.value === null ? null : (store.state.groupOps.items[opId.value] ?? null)));
const waiting = computed(() => busy.value || (op.value !== null && op.value.status !== 'done' && op.value.status !== 'failed' && op.value.status !== 'declined'));

let poll: ReturnType<typeof setInterval> | null = null;

function stopPoll(): void {
  if (poll !== null) clearInterval(poll);
  poll = null;
}

watch(op, (current) => {
  if (current === null) return;
  if (current.status === 'done') {
    stopPoll();
    if (current.link !== null && current.link !== '') link.value = current.link;
    else error.value = 'Ссылка не пришла — попробуйте ещё раз.';
  } else if (current.status === 'failed' || current.status === 'declined') {
    stopPoll();
    error.value = current.status === 'declined' ? 'Отменено.' : `Не вышло: ${groupOpErrorText(current.error)}.`;
  }
});

async function request(kind: 'link' | 'link_reset'): Promise<void> {
  busy.value = true;
  error.value = null;
  copied.value = null;
  try {
    const created = await store.submitGroupOp({ kind, channelId: props.channelId });
    opId.value = created.id;
    stopPoll();
    poll = setInterval(() => void store.refreshGroupOp(created.id).catch(() => undefined), POLL_MS);
  } catch (err) {
    error.value =
      err instanceof ApiError && [403, 404, 409, 422, 429].includes(err.status) && err.message
        ? err.message
        : 'Не отправилось — проверьте соединение и попробуйте ещё раз.';
  } finally {
    busy.value = false;
  }
}

function reset(): void {
  if (!window.confirm(`Сбросить ссылку «${props.title}»? Старая перестанет работать.`)) return;
  link.value = null;
  void request('link_reset');
}

async function copy(): Promise<void> {
  if (link.value === null) return;
  copied.value = await copyText(link.value);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  event.stopPropagation();
  emit('close');
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => dialog.value?.focus());
  void request('link');
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  stopPoll();
});
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay group-link-overlay"
      @click.self="emit('close')"
    >
      <div
        ref="dialog"
        class="channel-modal role-text-modal group-link-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="`Ссылка-приглашение: ${title}`"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">Ссылка-приглашение · {{ title }}</b>
          <span class="board-col-spacer" />
          <button
            type="button"
            class="channel-modal-close"
            aria-label="Закрыть"
            @click="emit('close')"
          >
            ×
          </button>
        </header>
        <div class="role-text-body group-modal-body">
          <p
            v-if="waiting && link === null"
            class="role-text-loading"
          >
            получаю ссылку…
          </p>
          <template v-if="link !== null">
            <input
              class="group-link-field"
              type="text"
              readonly
              :value="link"
              aria-label="Ссылка-приглашение"
              @focus="($event.target as HTMLInputElement).select()"
            >
            <div class="new-message-actions">
              <button
                type="button"
                class="group-link-copy"
                @click="copy"
              >
                Копировать
              </button>
              <button
                type="button"
                class="group-link-reset"
                :disabled="waiting"
                @click="reset"
              >
                Сбросить
              </button>
            </div>
            <p
              v-if="copied === true"
              class="new-message-hint"
            >
              Скопировано.
            </p>
            <p
              v-else-if="copied === false"
              class="new-message-hint"
            >
              Браузер не дал скопировать — выделите ссылку и скопируйте руками.
            </p>
          </template>
          <p
            v-if="error"
            class="new-message-error"
            role="alert"
          >
            {{ error }}
          </p>
          <div
            v-if="error && link === null"
            class="new-message-actions"
          >
            <button
              type="button"
              :disabled="waiting"
              @click="request('link')"
            >
              Повторить
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
