<script setup lang="ts">
// Ф41-6б (2.4): столбец беседы на «Главной». Лента (`MessageFeed`), под ней карточки черновиков беседы и поле ввода.
// Столбец НЕ меняет `currentChannelId`, `homeOpen`, `openThread`: страницу сообщений грузит `loadChannelPage`.
// Беседа читается (отметка «прочитано»), только пока указатель над столбцом или фокус внутри; иначе в шапке растёт
// «N новых». Активный столбец — и присутствие владельца (`setBoardActiveChannel`). Тред и «Ответить» в Slack/Mattermost
// ведут на вкладку проекта (инъекция `IN_BOARD_COLUMN`). Все названия — чужой ввод: только текстом.
import { computed, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue';

import type { ColumnItem } from '../store/board';
import { store } from '../store/instance';
import { ATTENTION_LABELS } from '../util/attention';
import { IN_BOARD_COLUMN } from '../util/boardCtx';
import { sourceBadge } from '../util/people';
import BoardColumnShell from './BoardColumnShell.vue';
import ChannelPane from './ChannelPane.vue';
import ChannelView from './ChannelView.vue';

const props = defineProps<{ item: ColumnItem; channelId: number }>();

provide(IN_BOARD_COLUMN, true);

const channel = computed(() => store.state.sync.channelsById[props.channelId] ?? null);
const projectName = computed(() => {
  const id = channel.value?.project_id ?? null;
  if (id === null || id <= 0) return 'Неразобранное';
  return store.state.projects.find((p) => p.id === id)?.name ?? null;
});
// Записи беседы ещё нет (страница только что перезагружена) — «…», а не номер.
const title = computed(() => (channel.value === null ? '…' : (channel.value.title ?? `беседа #${props.channelId}`)));
const unread = computed(() => channel.value?.unread ?? 0);
const attentionWord = computed(() => {
  const c = channel.value;
  if (c === null) return null;
  const mode = c.attention_manual ?? c.attention_auto;
  return mode === undefined || mode === null ? null : ATTENTION_LABELS[mode];
});
const stage = computed(() => channel.value?.stage ?? null);

// Активность столбца: указатель над ним ИЛИ фокус внутри.
const hovered = ref(false);
const focused = ref(false);
const active = computed(() => hovered.value || focused.value);

watch(active, (value) => store.setBoardActiveChannel(props.channelId, value));

function onFocusOut(event: FocusEvent): void {
  const root = event.currentTarget as HTMLElement | null;
  const next = event.relatedTarget;
  if (root !== null && next instanceof Node && root.contains(next)) return;
  focused.value = false;
}

function allChannels(): void {
  void store.openChannelInProject(props.channelId);
}

onMounted(() => {
  void store.ensureChannel(props.channelId);
  void store.loadChannelPage(props.channelId).catch(() => undefined);
  void store.loadDrafts();
});

onBeforeUnmount(() => store.setBoardActiveChannel(props.channelId, false));
</script>

<template>
  <BoardColumnShell
    :item="item"
    :project="null"
    :title="title"
    fill
    @pointerenter="hovered = true"
    @pointerleave="hovered = false"
    @focusin="focused = true"
    @focusout="onFocusOut"
  >
    <template #head-extra>
      <span
        v-if="channel?.source"
        class="source-label board-channel-source"
      >{{ sourceBadge(channel.source) }}</span>
      <span
        v-if="unread > 0"
        class="board-channel-unread"
      >{{ unread }} новых</span>
    </template>
    <!-- Ф80-1 D1: те же действия, что в шапке беседы во вкладке проекта (проект, стадия, режим, «Без подтверждения»,
         «Напомнить…», «Заглушить», «Удалить…», «Выйти»). -->
    <template #menu-extra="{ close }">
      <p
        v-if="projectName || attentionWord || stage"
        class="board-channel-info"
      >
        {{ [projectName, attentionWord, stage].filter(Boolean).join(' · ') }}
      </p>
      <button
        type="button"
        role="menuitem"
        class="board-channel-all"
        @click="close(); allChannels()"
      >
        Открыть в проекте{{ projectName ? ` ${projectName}` : '' }}
      </button>
      <ChannelView
        :channel-id="channelId"
        actions-only
      />
    </template>
    <div
      class="board-channel"
      :data-active="active ? '1' : '0'"
    >
      <ChannelPane
        :channel-id="channelId"
        :active="active"
      />
    </div>
  </BoardColumnShell>
</template>
