<script setup lang="ts">
// Ф38-2 (`docs/architecture.md`): карточка «Завести правило?» — сессия просит правило
// раскладки (источник · аккаунт: условие → проект), «переложит N бесед: …», от кого и зачем; «Завести» и «Отклонить».
// После решения — итог («заведено, переложено N» / «отклонено»). Описание, причина, имя сессии, названия бесед и
// аккаунта пишут посторонние люди и модели — только текстовой интерполяцией.
import { computed, ref } from 'vue';

import type { RuleRequest } from '../api/types';
import { store } from '../store/instance';
import { projectName } from '../util/mapping';
import { ruleHeadline, ruleMovedLine, ruleOutcomeLine } from '../util/ruleRequests';

const props = defineProps<{ request: RuleRequest }>();

const busy = ref(false);
const error = ref<string | null>(null);

const isPending = computed(() => props.request.status === 'pending');
const headline = computed(() => ruleHeadline(props.request, projectName(store.state.projects, props.request.project_id)));
const movedLine = computed(() => (isPending.value ? ruleMovedLine(props.request) : null));
const outcome = computed(() => ruleOutcomeLine(props.request));

async function decide(decision: 'approve' | 'decline'): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const result = await store.decideRuleRequest(props.request, decision);
    if (result === 'stale') error.value = 'Заявка уже изменилась — обновил.';
    else if (result === 'error') error.value = 'Не отправилось — попробуй ещё раз.';
    else if (typeof result === 'object') error.value = result.detail;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div
    class="rule-request-card"
    :data-rule-request-id="request.id"
  >
    <p
      v-if="request.auto && request.status !== 'pending'"
      class="auto-accepted rule-request-auto"
    >
      принято автопилотом
    </p>
    <p class="rule-request-head">
      <strong>Правило раскладки</strong>
    </p>
    <p class="rule-request-line">
      {{ headline }}
    </p>
    <p
      v-if="movedLine"
      class="rule-request-moved"
    >
      {{ movedLine }}
    </p>
    <p
      v-if="request.who"
      class="rule-request-meta"
    >
      от «{{ request.who }}»
    </p>
    <p
      v-if="request.reason"
      class="rule-request-meta"
    >
      Причина: {{ request.reason }}
    </p>

    <p
      v-if="outcome"
      class="rule-request-outcome"
    >
      {{ outcome }}
    </p>

    <div
      v-if="isPending"
      class="rule-request-actions"
    >
      <button
        type="button"
        class="rule-request-approve"
        :disabled="busy"
        @click="decide('approve')"
      >
        Завести
      </button>
      <button
        type="button"
        class="rule-request-decline"
        :disabled="busy"
        @click="decide('decline')"
      >
        Отклонить
      </button>
    </div>
    <p
      v-if="error"
      class="rule-request-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>
