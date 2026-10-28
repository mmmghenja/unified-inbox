<script setup lang="ts">
// Беседы одной группы внимания (Ф8-6 C3), разложенные по «Чаты / Почта / Задачи» (план, раздел 6).
import { computed } from 'vue';

import type { Channel } from '../api/types';
import ChannelNo from './ChannelNo.vue';
import { store } from '../store/instance';
import { unreadBadge, type UnreadBadge } from '../util/attention';
import { CHANNEL_GROUP_ORDER, groupChannels } from '../util/channelGroup';
import { sourceBadge, sourceLabel } from '../util/people';
import { hasStage, stageChipTitle } from '../util/stages';
import { freshThreadLabel, threadTargetOf } from '../util/threadNav';

// Метка источника: короткий значок в строке («MM», «SL», «TG», «@»), полное название — в подсказке. Аккаунт беседы в
// данных строки не приходит — в подсказке только источник.
function sourceTitle(channel: Channel): string {
  return `Источник: ${sourceLabel(channel.source ?? '')}`;
}

// Ф14-1 C2: в режиме выбора у строк — отметка, клик по строке выбирает/снимает (беседа не открывается).
const props = defineProps<{ channels: Channel[]; selecting?: boolean; selected?: ReadonlySet<number> }>();
const emit = defineEmits<{ toggle: [channelId: number] }>();

function isSelected(channelId: number): boolean {
  return props.selected?.has(channelId) ?? false;
}

// Спека 1d-1, п.5: топик подписан «<форум> › <тема>» (parent_title приходит только у kind='topic').
function displayTitle(channel: Channel): string {
  const title = channel.title ?? '(без названия)';
  if (channel.kind === 'topic' && channel.parent_title) {
    return `${channel.parent_title} › ${title}`;
  }
  return title;
}

// Спека 2e, раздел «Список каналов и проекты»: `unread_top` — основное число (как раньше `unread`); если
// поле не пришло (старое ядро) — как раньше, по `unread` целиком (оно и раньше было суммой без тредов).
function primaryUnread(channel: Channel): number {
  return channel.unread_top ?? channel.unread;
}

// Ф8-3 D2: требующее реакции — красный счётчик (`unread_react`), прочее — серый; старое ядро — как раньше.
function badgeOf(channel: Channel): UnreadBadge | null {
  return unreadBadge(channel.unread_react, primaryUnread(channel));
}

const grouped = computed(() => groupChannels(props.channels));

function select(channelId: number): void {
  if (props.selecting) {
    emit('toggle', channelId);
    return;
  }
  // Ф66-5: нового в основной ленте нет, а внутри есть свежий тред — сразу панель этого треда.
  void store.openChannel(channelId, { thread: threadTargetOf(store.state.sync.channelsById[channelId]) });
}
</script>

<template>
  <template
    v-for="group in CHANNEL_GROUP_ORDER"
    :key="group"
  >
    <h3
      v-if="(grouped.get(group)?.length ?? 0) > 0"
      class="channel-group-title"
    >
      {{ group }}
    </h3>
    <ul v-if="(grouped.get(group)?.length ?? 0) > 0">
      <li
        v-for="channel in grouped.get(group)"
        :key="channel.id"
      >
        <button
          type="button"
          class="channel-button"
          :class="{
            active: !selecting && channel.id === store.state.currentChannelId,
            muted: channel.muted,
            'channel-button--selected': selecting && isSelected(channel.id),
          }"
          :role="selecting ? 'checkbox' : undefined"
          :aria-checked="selecting ? isSelected(channel.id) : undefined"
          :data-channel-id="channel.id"
          @click="select(channel.id)"
        >
          <span
            v-if="selecting"
            class="channel-check"
            aria-hidden="true"
          >{{ isSelected(channel.id) ? '☑' : '☐' }}</span>
          <span class="channel-title">{{ displayTitle(channel) }}</span>
          <ChannelNo :id="channel.id" />
          <span
            v-if="channel.source"
            class="source-badge"
            :title="sourceTitle(channel)"
          >{{ sourceBadge(channel.source) }}</span>
          <span
            v-if="hasStage(channel)"
            class="stage-chip"
            :title="stageChipTitle(channel)"
          >{{ channel.stage }}</span>
          <span
            v-if="channel.new_to"
            class="badge badge-new-channel"
            title="Новая беседа: первое сообщение ещё не ушло"
          >новая</span>
          <span
            v-if="badgeOf(channel) !== null"
            class="badge"
            :class="`badge-${badgeOf(channel)?.kind}`"
          >{{ badgeOf(channel)?.count }}</span>
          <span
            v-if="(channel.unread_threads ?? 0) > 0"
            class="badge badge-threads"
          >💬{{ channel.unread_threads }}</span>
          <span
            v-else-if="freshThreadLabel(channel) !== null"
            class="badge badge-fresh-thread"
            title="Внутри беседы есть тред с новыми ответами"
          >{{ freshThreadLabel(channel) }}</span>
        </button>
      </li>
    </ul>
  </template>
</template>
