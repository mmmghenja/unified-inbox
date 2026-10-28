<script setup lang="ts">
// Общая середина беседы на «Главной» (столбец `BoardChannel` и окно `ChannelModal`): лента, карточки черновиков
// беседы, поле ввода или `NoReplyNotice`. Активность (чтение) задаёт хозяин через `active`.
import { computed, ref } from 'vue';

import type { Message } from '../api/types';
import { store } from '../store/instance';
import { replyPreviewLabel } from '../util/replyPreview';
import Composer from './Composer.vue';
import DraftCard from './DraftCard.vue';
import MessageFeed from './MessageFeed.vue';
import NoReplyNotice from './NoReplyNotice.vue';

const props = defineProps<{ channelId: number; active: boolean }>();

const channel = computed(() => store.state.sync.channelsById[props.channelId] ?? null);
const canReply = computed(() => channel.value?.can_reply !== false);
const pinnedDrafts = computed(() => store.draftsOf(props.channelId));

// Режим ответа Telegram-стиля — как в `ChannelView`: плашка над полем ввода.
const replyTarget = ref<{ extId: string; preview: string } | null>(null);

function onReply(message: Message): void {
  if (message.ext_id === null) return;
  replyTarget.value = { extId: message.ext_id, preview: replyPreviewLabel(message) };
}
</script>

<template>
  <MessageFeed
    :channel-id="channelId"
    :read-enabled="active"
    @reply="onReply"
  />
  <div
    v-if="pinnedDrafts.length > 0"
    class="pinned-drafts"
    aria-label="Черновики беседы"
  >
    <DraftCard
      v-for="draft in pinnedDrafts"
      :key="draft.id"
      :item="draft"
      :show-address="false"
      compact
    />
  </div>
  <Composer
    v-if="canReply"
    :channel-id="channelId"
    :reply-to="replyTarget"
    @cancel-reply="replyTarget = null"
  />
  <NoReplyNotice
    v-else
    :channel="channel"
  />
</template>
