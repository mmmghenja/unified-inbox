<script setup lang="ts">
// Окно беседы поверх «Главной» компьютера (клик по беседе в «Ждёт тебя», на карточках и т. п. — «для ознакомления»).
// Внутри то же, что в столбце (`ChannelPane`): лента, черновики беседы, поле ввода. Беседа читается, пока окно
// открыто и вкладка видна; закрытие снимает активность. `currentChannelId`, `homeOpen`, `openThread` окно не
// меняет; тред и «Ответить» ведут на вкладку проекта (`IN_BOARD_COLUMN`) — тогда окно закрывается само.
// Закрывается по ✕, Esc и клику мимо. Названия — чужой ввод: только текстом.
import { computed, nextTick, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue';

import { store } from '../store/instance';
import { ATTENTION_LABELS } from '../util/attention';
import { IN_BOARD_COLUMN } from '../util/boardCtx';
import { sourceBadge } from '../util/people';
import { openPopupCount, usePopup } from '../util/popup';
import ChannelNo from './ChannelNo.vue';
import ChannelPane from './ChannelPane.vue';
import ChannelView from './ChannelView.vue';

const props = defineProps<{ channelId: number }>();

provide(IN_BOARD_COLUMN, true);

const dialog = ref<HTMLElement | null>(null);
// Ф80-1 D1: «⋯» — те же действия с беседой, что во вкладке проекта (общий механизм меню `util/popup`).
const menuOpen = ref(false);
const menuButton = ref<HTMLElement | null>(null);
const menuPanel = ref<HTMLElement | null>(null);
usePopup({ open: menuOpen, button: () => menuButton.value, panel: () => menuPanel.value });
const visible = ref(typeof document === 'undefined' ? true : document.visibilityState !== 'hidden');
const active = computed(() => visible.value);

const channel = computed(() => store.state.sync.channelsById[props.channelId] ?? null);
const projectName = computed(() => {
  const id = channel.value?.project_id ?? null;
  if (id === null || id <= 0) return 'Неразобранное';
  return store.state.projects.find((p) => p.id === id)?.name ?? null;
});
const title = computed(() => (channel.value === null ? '…' : (channel.value.title ?? `беседа #${props.channelId}`)));
const attentionWord = computed(() => {
  const c = channel.value;
  if (c === null) return null;
  const mode = c.attention_manual ?? c.attention_auto;
  return mode === undefined || mode === null ? null : ATTENTION_LABELS[mode];
});
const stage = computed(() => channel.value?.stage ?? null);

function close(): void {
  store.closeChannelModal();
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  if (openPopupCount() > 0) return; // сначала закрывается открытое меню, потом окно
  event.stopPropagation();
  close();
}

function onVisibility(): void {
  visible.value = document.visibilityState !== 'hidden';
}

function allChannels(): void {
  if (store.state.channelModalOverlay) close();
  void store.openChannelInProject(props.channelId);
}

watch(active, (value) => store.setBoardActiveChannel(props.channelId, value), { immediate: true });
// Уход с главной (тред, «все беседы», вкладка проекта) — окно закрывается.
watch(() => store.state.homeOpen, (open) => { if (!open && !store.state.channelModalOverlay) close(); });

onMounted(() => {
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('visibilitychange', onVisibility);
  void store.ensureChannel(props.channelId);
  void store.loadChannelPage(props.channelId).catch(() => undefined);
  void store.loadDrafts();
  void nextTick(() => dialog.value?.focus());
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  document.removeEventListener('visibilitychange', onVisibility);
  store.setBoardActiveChannel(props.channelId, false);
});
</script>

<template>
  <div
    class="channel-modal-overlay"
    @click.self="close"
  >
    <div
      ref="dialog"
      class="channel-modal"
      role="dialog"
      aria-modal="true"
      :aria-label="`Беседа: ${title}`"
      tabindex="-1"
    >
      <header class="channel-modal-head">
        <button
          type="button"
          class="channel-modal-close"
          aria-label="Закрыть"
          @click="close"
        >
          ×
        </button>
        <span
          class="channel-modal-title"
          :title="projectName ? `${projectName} / ${title}` : title"
        >
          <b class="board-col-name">{{ title }}</b>
          <ChannelNo :id="channelId" />
        </span>
        <span
          v-if="channel?.source"
          class="source-label board-channel-source"
        >{{ sourceBadge(channel.source) }}</span>
        <span class="board-col-spacer" />
        <span class="channel-modal-menu-anchor">
          <button
            ref="menuButton"
            type="button"
            class="channel-modal-menu-button"
            aria-label="Действия с беседой"
            :aria-expanded="menuOpen"
            @click="menuOpen = !menuOpen"
          >
            ⋯
          </button>
          <div
            v-if="menuOpen"
            ref="menuPanel"
            class="board-col-popup channel-modal-menu"
            role="menu"
          >
            <p
              v-if="projectName || attentionWord || stage"
              class="board-channel-info"
            >
              {{ [projectName, attentionWord, stage].filter(Boolean).join(' · ') }}
            </p>
            <button
              v-if="!store.state.channelModalOverlay"
              type="button"
              role="menuitem"
              class="channel-modal-board"
              @click="store.channelModalToColumn()"
            >
              На доску
            </button>
            <button
              type="button"
              role="menuitem"
              class="channel-modal-project"
              @click="allChannels"
            >
              Открыть в проекте
            </button>
            <ChannelView
              :channel-id="channelId"
              actions-only
            />
          </div>
        </span>
      </header>
      <div
        class="board-channel channel-modal-body"
        :data-active="active ? '1' : '0'"
      >
        <ChannelPane
          :channel-id="channelId"
          :active="active"
        />
      </div>
    </div>
  </div>
</template>
