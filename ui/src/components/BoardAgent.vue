<script setup lang="ts">
// Ф41-6a/6б (2.4): столбец внешней сессии (VS Code / терминал / OpenCode): писать ей можно только как агенту —
// диалог `AgentDialog`. («Забрать в хаб» убрано в Ф33-0.)
import { computed } from 'vue';

import type { OfflineLine } from '../api/types';
import type { ColumnItem } from '../store/board';
import { store } from '../store/instance';
import { engineLabel, listeningLabel, type AgentLine } from '../store/peerMsgs';
import { offlineName } from '../util/boardList';
import AgentDialog from './AgentDialog.vue';
import AgentUnsubscribe from './AgentUnsubscribe.vue';
import BoardColumnShell from './BoardColumnShell.vue';
import MachineChip from './MachineChip.vue';
import SessionCacheLine from './SessionCacheLine.vue';
import SessionTail from './SessionTail.vue';

const props = defineProps<{ item: ColumnItem; line: AgentLine | null; offline?: OfflineLine | null; nowMs?: number; fixed?: boolean }>();

// Линия для диалога: живая/диалоговая из списка агентов, иначе собранная из строки «не в сети».
const effectiveLine = computed<AgentLine | null>(() => {
  if (props.line !== null) return props.line;
  const o = props.offline ?? null;
  if (o === null) return null;
  return {
    key: o.key,
    projectId: o.project_id,
    name: offlineName(o),
    hasRole: o.role !== null && o.role.trim() !== '',
    address: { project_id: o.project_id, role: o.role ?? '', number: o.role_no ?? 1 },
    sub: null,
    cwd: null,
    unread: 0,
  };
});

const project = computed(() => {
  const id = effectiveLine.value?.projectId;
  if (id === undefined) return null;
  return id === 0 ? 'Вне проектов' : (store.state.projects.find((p) => p.id === id)?.name ?? null);
});
const sub = computed(() => props.line?.sub ?? null);
const chip = computed(() => (sub.value === null ? 'не в сети' : listeningLabel(sub.value)));
const tone = computed<'wait' | 'run' | 'idle'>(() => (sub.value?.listening ? 'run' : 'idle'));
</script>

<template>
  <BoardColumnShell
    :item="item"
    :fixed="fixed"
    :project="project"
    :title="effectiveLine?.name ?? 'сессия'"
    :chip="effectiveLine ? chip : null"
    :chip-tone="tone"
  >
    <template #head-extra>
      <MachineChip :machine="sub?.machine" />
    </template>
    <div
      v-if="effectiveLine"
      class="board-agent"
    >
      <p class="board-agent-note">
        Сессия открыта не в хабе ({{ sub ? engineLabel(sub.engine) : offline ? `${engineLabel(offline.engine)}, не в сети` : 'не в сети' }}): писать ей можно как агенту.
      </p>
      <SessionCacheLine
        v-if="sub?.cache"
        :cache="sub.cache"
        :now-ms="nowMs"
      />
      <AgentUnsubscribe
        v-if="sub !== null"
        :sub-id="sub.id"
      />
      <SessionTail
        v-if="sub !== null"
        :sub-id="sub.id"
      />
      <AgentDialog :line="effectiveLine" />
    </div>
    <p
      v-else
      class="board-empty"
    >
      Эта сессия сейчас не в сети.
    </p>
  </BoardColumnShell>
</template>
