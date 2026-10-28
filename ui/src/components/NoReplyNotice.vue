<script setup lang="ts">
// Ф10-5 B5: беседа уведомлений трекера (`can_reply === false`) — вместо поля ввода строка и ссылка в
// трекер. Ссылка — только `https://` (ядро другое и не отдаёт, здесь — вторая защита).
// Ф12-1 C2: беседа-заготовка черновика новому адресату (`new_to`) — своя строка, без ссылки.
// Ф12-5 C3: у беседы-заготовки строка — причина из ядра (`can_reply_reason`: первое сообщение в пути / ушло
// без привязки), не зашитый текст; прежняя строка — только если ядро причину не прислало.
import { computed } from 'vue';

import type { Channel } from '../api/types';

const props = defineProps<{ channel: Channel | null }>();

const isNew = computed(() => Boolean(props.channel?.new_to));
const newReason = computed(
  () => props.channel?.can_reply_reason || 'Новая беседа: сообщение уйдёт после подтверждения черновика',
);

const link = computed(() => {
  if (isNew.value) return null;
  const tracker = props.channel?.tracker;
  if (!tracker || typeof tracker.url !== 'string' || !tracker.url.startsWith('https://')) return null;
  const labels = { trello: 'Открыть в Trello', notify: 'Открыть на сайте', jira: 'Открыть в Jira' } as const;
  return { url: tracker.url, label: labels[tracker.name] ?? 'Открыть в Jira' };
});
</script>

<template>
  <div
    class="composer no-reply"
    role="note"
  >
    <span v-if="isNew">{{ newReason }}</span>
    <span v-else>Ответ в трекер из хаба не уходит</span>
    <a
      v-if="link"
      class="no-reply-link"
      :href="link.url"
      target="_blank"
      rel="noopener noreferrer"
    >{{ link.label }}</a>
  </div>
</template>
