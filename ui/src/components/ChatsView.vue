<script setup lang="ts">
// Ф10-6 B3: беседы проекта на трёх ширинах.
// - `full`: список 260 px | лента | тред 360 px (когда открыт);
// - `half`: список `clamp(220px, 30%, 300px)` | лента; тред — поверх правой части ленты (закрывается × и Esc);
// - `phone`: один экран за раз — список -> лента («‹» к списку) -> тред («‹» к ленте). Лента под открытым
//   тредом остаётся смонтированной (скрыта стилем) — черновик в поле ввода и прокрутка не теряются.
import { computed } from 'vue';

import { store } from '../store/instance';
import { layout } from '../util/layout';
import ChannelList from './ChannelList.vue';
import ChannelView from './ChannelView.vue';
import ThreadPanel from './ThreadPanel.vue';

const mode = computed(() => layout.mode);
const channelId = computed(() => store.state.currentChannelId);
const threadOpen = computed(() => store.state.openThread !== null);
</script>

<template>
  <div
    class="chats-view"
    :class="[`chats-view--${mode}`, { 'chats-view--thread': threadOpen, 'chats-view--channel': channelId !== null }]"
  >
    <ChannelList v-if="store.state.currentProjectId !== null && (mode !== 'phone' || channelId === null)" />
    <div
      v-if="mode !== 'phone' || channelId !== null"
      class="chats-main"
    >
      <ChannelView
        v-if="channelId !== null"
        :channel-id="channelId"
      />
      <p
        v-else
        class="column placeholder-column"
      >
        Выберите беседу.
      </p>
      <ThreadPanel v-if="mode === 'half'" />
    </div>
    <ThreadPanel v-if="mode !== 'half'" />
  </div>
</template>
