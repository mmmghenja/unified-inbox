<script setup lang="ts">
// Ф44-1d (`docs/architecture.md`): карточка «Нажать?» — сессия просит нажать
// кнопку бота Telegram от имени владельца: что за кнопка, в какой беседе, кто просит (роль), цитата сообщения бота.
// «Нажать» и «Отказать» — только из интерфейса (в том числе с телефона); «К беседе» открывает беседу окном.
// Текст кнопки, роль, название беседы и цитата — чужой ввод: только текстовой интерполяцией.
import { computed, ref } from 'vue';

import type { ButtonPress } from '../api/types';
import { store } from '../store/instance';
import { botGuardNote, pressHeadline } from '../util/presses';

const props = defineProps<{ press: ButtonPress }>();

const guardNote = computed(() => botGuardNote(props.press.reason, true));
const busy = ref(false);
const error = ref<string | null>(null);

async function decide(decision: 'approve' | 'decline'): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decidePress(props.press, decision);
    if (result === 'stale') error.value = 'Нажатие уже изменилось — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуй ещё раз.';
  } finally {
    busy.value = false;
  }
}

function openChannel(): void {
  const projectId = props.press.project_id;
  void store.openChannelAt(projectId !== null && projectId > 0 ? projectId : null, props.press.channel_id, {
    messageId: props.press.message_id,
  });
}
</script>

<template>
  <div
    class="press-card"
    :data-press-id="press.id"
  >
    <p class="press-head">
      <strong>{{ pressHeadline(press) }}</strong>
    </p>
    <p
      v-if="press.quote"
      class="press-quote"
    >
      {{ press.quote }}
    </p>
    <p
      v-if="guardNote"
      class="press-guard"
      data-bot-guard
    >
      {{ guardNote }}
    </p>
    <p class="press-note">
      бот выполнит действие от твоего имени
    </p>
    <div class="press-actions">
      <button
        type="button"
        class="press-approve"
        :disabled="busy"
        @click="decide('approve')"
      >
        Нажать
      </button>
      <button
        type="button"
        class="press-decline"
        :disabled="busy"
        @click="decide('decline')"
      >
        Отказать
      </button>
      <button
        type="button"
        class="press-open"
        @click="openChannel"
      >
        К беседе
      </button>
    </div>
    <p
      v-if="error"
      class="join-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
