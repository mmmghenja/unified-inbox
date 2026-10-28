<script setup lang="ts">
// Ф17-1b (`docs/architecture.md`): карточка заявки сессии на вступление в группу или канал
// Telegram: что это (название, «канал · N подписчиков»), «нужно одобрение админов», описание (2 строки), «Зачем» и
// от кого; «Вступить» / «Отказать» (отказ — пока шаг вступления не выдавался, `can_decline`). После решения владельца
// карточка сжимается в одну строку «название · исход»: «вступаю…», `failed` — причина простыми словами,
// `requested` — «заявка у админов» (видны сутки); в «Ждёт тебя» таких строк нет — только в столбце проекта.
// Название, описание, причина и имя клиента пишут посторонние люди и сессия — только текстовой интерполяцией.
import { computed, ref } from 'vue';

import type { JoinRequest } from '../api/types';
import { store } from '../store/instance';
import { failReason, joinKindLine, joinTitle } from '../util/joins';

const props = defineProps<{ join: JoinRequest }>();

const busy = ref(false);
const error = ref<string | null>(null);

const title = computed(() => joinTitle(props.join));
const kindLine = computed(() => joinKindLine(props.join));
const isPending = computed(() => props.join.status === 'pending');
const isApproved = computed(() => props.join.status === 'approved');
const outcome = computed(() => {
  if (props.join.status === 'failed') return `не вступили: ${failReason(props.join.error)}`;
  if (props.join.status === 'requested') return 'заявка у админов';
  return isApproved.value ? 'вступаю…' : '';
});
const about = computed(() => props.join.peek?.about?.trim() ?? '');
// Ф17-3: группа обсуждения канала / канал группы — одной серой строкой (названия пишут посторонние люди).
const linkedLine = computed(() => {
  const linked = props.join.peek?.linked;
  if (!linked || !linked.title) return '';
  const handle = linked.username ? ` (@${linked.username})` : '';
  return linked.kind === 'channel'
    ? `это группа обсуждения канала «${linked.title}»${handle}`
    : `группа обсуждения: «${linked.title}»${handle}`;
});

async function decide(decision: 'approve' | 'decline'): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideJoin(props.join, decision);
    if (result === 'stale') error.value = 'Заявка уже изменилась — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуй ещё раз.';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="join-card"
    :class="{ 'join-card--line': !isPending }"
    :data-join-id="join.id"
  >
    <p
      v-if="join.auto && join.status !== 'pending'"
      class="auto-accepted join-auto"
    >
      принято автопилотом
    </p>
    <p class="join-head">
      <strong class="join-title">{{ title }}</strong>
      <template v-if="isPending">
        <span
          v-if="kindLine"
          class="join-kind"
        >{{ kindLine }}</span>
        <span
          v-if="join.peek?.request_needed"
          class="join-flag"
        >нужно одобрение админов</span>
      </template>
      <span
        v-else-if="outcome"
        class="join-outcome"
        :class="{ 'join-outcome--failed': join.status === 'failed' }"
      >{{ outcome }}</span>
    </p>
    <template v-if="isPending">
      <p
        v-if="about"
        class="join-about"
      >
        {{ about }}
      </p>
      <p
        v-if="linkedLine"
        class="join-reason join-linked"
      >
        {{ linkedLine }}
      </p>
      <p
        v-if="join.reason"
        class="join-reason"
      >
        Зачем: {{ join.reason }}<template v-if="join.client_name">
          · {{ join.client_name }}
        </template>
      </p>
      <p
        v-else-if="join.client_name"
        class="join-reason"
      >
        {{ join.client_name }}
      </p>
    </template>

    <div
      v-if="isPending || (isApproved && join.can_decline)"
      class="join-actions"
    >
      <button
        v-if="isPending"
        type="button"
        class="join-approve"
        :disabled="busy"
        @click="decide('approve')"
      >
        Вступить
      </button>
      <button
        v-if="join.can_decline"
        type="button"
        class="join-decline"
        :disabled="busy"
        @click="decide('decline')"
      >
        Отказать
      </button>
    </div>
    <p
      v-if="error"
      class="join-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
