<script setup lang="ts">
// Ф41-6a (2.4): столбец сессии хаба — `SessionWindow` в режиме столбца. Пока столбец смонтирован, окно считается
// раскрытым (стор дочитывает события и держит поток «пишет…»).
// Ф90-1: шапка — одна строка: точка состояния (подпись по наведению), имя, чип «авто», «⋯». Проект, модель, запасной,
// машина, фоновые, подпапка, кэш и подписка — в «Сведениях» меню (`SessionMenuItems`).
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import type { HubSession } from '../api/types';
import type { ColumnItem } from '../store/board';
import { store } from '../store/instance';
import { sessionHeadState } from '../util/sessionHead';
import { sessionName } from '../util/sessions';
import BoardColumnShell from './BoardColumnShell.vue';
import type { SessionWindowHandle } from '../util/sessionHandle';
import SessionMenuItems from './SessionMenuItems.vue';
import SessionWindow from './SessionWindow.vue';

const props = defineProps<{ item: ColumnItem; session: HubSession; fixed?: boolean }>();

const win = ref<SessionWindowHandle | null>(null);
const head = computed(() =>
  sessionHeadState(props.session, store.state.sessions.pending[props.session.id]?.length ?? props.session.pending_requests),
);

onMounted(() => {
  store.state.sessions.expanded[props.session.id] = true;
  void store.refreshSession(props.session.id);
});
onBeforeUnmount(() => {
  store.state.sessions.expanded[props.session.id] = false;
});
</script>

<template>
  <BoardColumnShell
    :item="item"
    :fixed="fixed"
    :title="sessionName(session)"
    :dot="head.tone"
    :dot-text="head.text"
    :auto="session.mode === 'auto'"
    @auto="win?.chooseMode('manual')"
  >
    <template #menu-extra="{ close }">
      <SessionMenuItems
        :win="win"
        :session="session"
        @pick="close"
      />
    </template>
    <SessionWindow
      ref="win"
      :session="session"
      column
    />
  </BoardColumnShell>
</template>
