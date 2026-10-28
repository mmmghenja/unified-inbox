<script setup lang="ts">
// Ф10-6 B (`docs/architecture.md`): оболочка. `full`/`half` — верхняя полоса с вкладками проектов,
// под ней главная-доска или беседы проекта (`ChatsView`); `phone` — `PhoneShell` (нижние вкладки). Левой
// колонки проектов больше нет.
import { computed, onBeforeUnmount, onMounted, watch } from 'vue';

import { store } from '../store/instance';
import { peopleUi } from '../store/people';
import { installGlobalHotkeys } from '../util/hotkeyActions';
import { layout } from '../util/layout';
import { closeAllPopups } from '../util/popup';
import ChannelModal from './ChannelModal.vue';
import ChatsView from './ChatsView.vue';
import BoardView from './BoardView.vue';
import ClaudeView from './ClaudeView.vue';
import PersonCard from './PersonCard.vue';
import HotkeysHelp from './HotkeysHelp.vue';
import DraftModalHost from './DraftModalHost.vue';
import MdViewerModal from './MdViewerModal.vue';
import TerminalHost from './TerminalHost.vue';
import WriteModalHost from './WriteModalHost.vue';
import PhoneShell from './PhoneShell.vue';
import TopBar from './TopBar.vue';

const mode = computed(() => layout.mode);

// Ф80-1 A1: смена вкладки (проект, главная, «Агенты», вкладка телефона) закрывает открытые всплывающие меню.
watch(
  () => [store.state.currentProjectId, store.state.homeOpen, store.state.claudeOpen, store.state.phoneTab],
  () => closeAllPopups(),
);

const releaseHotkeys = installGlobalHotkeys();
onBeforeUnmount(releaseHotkeys);

onMounted(() => {
  void store.loadInbox();
  void store.refreshStatus();
});
</script>

<template>
  <div
    class="main-app"
    :class="`main-app--${mode}`"
  >
    <DraftModalHost />
    <WriteModalHost />
    <TerminalHost />
    <MdViewerModal />
    <HotkeysHelp />
    <PhoneShell v-if="mode === 'phone'" />
    <template v-else>
      <TopBar />
      <div class="shell-body">
        <BoardView v-if="store.state.homeOpen" />
        <ClaudeView v-else-if="store.state.claudeOpen" />
        <ChatsView v-else />
      </div>
      <ChannelModal
        v-if="store.state.channelModalId !== null"
        :key="store.state.channelModalId"
        :channel-id="store.state.channelModalId"
      />
    </template>
    <PersonCard
      v-if="peopleUi.card !== null"
      :target="peopleUi.card"
    />
  </div>
</template>
