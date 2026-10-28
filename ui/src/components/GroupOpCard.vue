<script setup lang="ts">
// Ф62-6 (раздел G): просьба сессии по группе WhatsApp — создать группу, добавить или убрать участников, сбросить
// ссылку. Вопрос карточки («Создать группу «…» с: …?»), кто просил и зачем; «Да» / «Отказать» (`can_decline` —
// пока задание не выдано коннектору). После «Да» — строка хода; после выполнения — итог по участникам. Название,
// причина и имена — чужой текст, только интерполяция. По образцу `LeaveCard.vue`.
import { computed, ref } from 'vue';

import type { GroupOp } from '../api/types';
import { store } from '../store/instance';
import { groupOpOutcomeLines, groupOpProgress, groupOpQuestion } from '../util/groupOps';
import ErrandBlock from './ErrandBlock.vue';

const props = defineProps<{ op: GroupOp }>();

const busy = ref(false);
const error = ref<string | null>(null);

const channelTitle = computed(() =>
  props.op.channel_id === null ? null : (store.state.sync.channelsById[props.op.channel_id]?.title ?? null),
);
const question = computed(() => groupOpQuestion(props.op, channelTitle.value));
const outcome = computed(() => groupOpOutcomeLines(props.op));
const isPending = computed(() => props.op.status === 'pending');
const isApproved = computed(() => props.op.status === 'approved');

function open(): void {
  if (props.op.channel_id !== null) void store.openChannelAt(props.op.project_id, props.op.channel_id);
}

async function decide(decision: 'approve' | 'decline'): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideGroupOp(props.op, decision);
    if (result === 'stale') error.value = 'Заявка уже изменилась — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуй ещё раз.';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="group-op-card"
    :data-group-op-id="op.id"
  >
    <p
      v-if="op.auto && op.status !== 'pending'"
      class="auto-accepted group-op-auto"
    >
      принято автопилотом
    </p>
    <p class="group-op-head">
      <button
        v-if="op.channel_id !== null"
        type="button"
        class="group-op-question group-op-question--link"
        @click="open"
      >
        {{ question }}
      </button>
      <span
        v-else
        class="group-op-question"
      >{{ question }}</span>
    </p>
    <p
      v-if="op.reason"
      class="group-op-reason"
    >
      Зачем: {{ op.reason }}{{ op.client_name ? ` · ${op.client_name}` : '' }}
    </p>
    <p
      v-else-if="op.client_name"
      class="group-op-reason"
    >
      {{ op.client_name }}
    </p>
    <p
      v-if="isPending && op.error"
      class="group-op-hold"
    >
      {{ op.error }}
    </p>
    <p
      v-if="isApproved"
      class="group-op-outcome"
    >
      {{ groupOpProgress(op) }}
    </p>
    <ul
      v-if="outcome.length > 0"
      class="group-op-result"
    >
      <li
        v-for="line in outcome"
        :key="line"
      >
        {{ line }}
      </li>
    </ul>

    <ErrandBlock
      v-if="isPending"
      :item-ref="`group_op:${op.id}`"
      :title="question"
      test-prefix="group-op"
    />
    <div
      v-if="isPending || (isApproved && op.can_decline)"
      class="group-op-actions"
    >
      <button
        v-if="isPending"
        type="button"
        class="group-op-approve"
        :disabled="busy"
        @click="decide('approve')"
      >
        Да
      </button>
      <button
        v-if="op.can_decline"
        type="button"
        class="group-op-decline"
        :disabled="busy"
        @click="decide('decline')"
      >
        Отказать
      </button>
    </div>
    <p
      v-if="error"
      class="group-op-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
