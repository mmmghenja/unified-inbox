<script setup lang="ts">
// Ф17-2b (`docs/architecture.md`): просьба сессии выйти из группы или канала Telegram:
// название беседы (клик открывает её), вид, кто просил и зачем; «Выйти» / «Оставить» (`can_decline` — пока выход
// не выдан коннектору). После «Выйти» — «выхожу…». Название и причина — чужой текст, только интерполяция.
import { computed, ref } from 'vue';

import type { LeaveRequest } from '../api/types';
import { store } from '../store/instance';
import { leaveKindLabel, leaveTitle } from '../util/leaves';
import ErrandBlock from './ErrandBlock.vue';

const props = defineProps<{ leave: LeaveRequest }>();

const busy = ref(false);
const error = ref<string | null>(null);

const title = computed(() => leaveTitle(props.leave));
const isPending = computed(() => props.leave.status === 'pending');
const isApproved = computed(() => props.leave.status === 'approved');

function open(): void {
  void store.openChannelAt(props.leave.project_id, props.leave.channel_id);
}

async function decide(decision: 'approve' | 'decline'): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideLeave(props.leave, decision);
    if (result === 'stale') error.value = 'Заявка уже изменилась — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуй ещё раз.';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="leave-card"
    :data-leave-id="leave.id"
  >
    <p
      v-if="leave.auto && leave.status !== 'pending'"
      class="auto-accepted leave-auto"
    >
      принято автопилотом
    </p>
    <p class="leave-head">
      <button
        type="button"
        class="leave-title"
        @click="open"
      >
        {{ title }}
      </button>
      <span class="leave-kind">{{ leaveKindLabel(leave) }}</span>
    </p>
    <p
      v-if="leave.reason"
      class="leave-reason"
    >
      Зачем: {{ leave.reason }}{{ leave.client_name ? ` · ${leave.client_name}` : '' }}
    </p>
    <p
      v-else-if="leave.client_name"
      class="leave-reason"
    >
      {{ leave.client_name }}
    </p>
    <p
      v-if="isApproved"
      class="leave-outcome"
    >
      выхожу…
    </p>

    <ErrandBlock
      v-if="isPending"
      :item-ref="`leave:${leave.id}`"
      :title="title"
      test-prefix="leave"
    />
    <div
      v-if="isPending || (isApproved && leave.can_decline)"
      class="leave-actions"
    >
      <button
        v-if="isPending"
        type="button"
        class="leave-approve"
        :disabled="busy"
        @click="decide('approve')"
      >
        Выйти
      </button>
      <button
        v-if="leave.can_decline"
        type="button"
        class="leave-decline"
        :disabled="busy"
        @click="decide('decline')"
      >
        Оставить
      </button>
    </div>
    <p
      v-if="error"
      class="leave-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
