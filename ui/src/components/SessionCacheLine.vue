<script setup lang="ts">
// Ф41-1: мелкая строка «кэш … · контекст …». Время тикает от `nowMs` родителя (своего таймера нет).
// Ф41-3а: у сессий хаба есть `usage` — «контекст 84 из 200 тыс. · кэш 58 мин · всего $1.20», контекст жёлтый выше
// 70 % предела и красный выше 90 %. Без `usage` — прежняя строка «кэш … · контекст …» по `cache`.
import { computed } from 'vue';

import type { SessionCache, SessionUsage } from '../api/types';
import type { CompactInfo } from '../util/sessionSteps';
import { cacheLabel, contextLabel } from '../util/sessionCache';
import { COST_TITLE_CLAUDE, usageLabel } from '../util/sessionSteps';

const props = defineProps<{ cache?: SessionCache | null; usage?: SessionUsage | null; engine?: string; nowMs?: number; compact?: CompactInfo }>();

const label = computed(() => (props.cache ? cacheLabel(props.cache, props.nowMs ?? Date.now()) : null));
const usageInfo = computed(() => (props.usage ? usageLabel(props.usage, props.compact) : null));
const costTitle = computed(() => (props.engine === 'claude' ? COST_TITLE_CLAUDE : undefined));
</script>

<template>
  <p
    v-if="usageInfo !== null"
    class="cache-line"
    :class="label ? `cache-line--${label.tone}` : undefined"
    :title="label?.title"
  >
    <span
      class="cache-ctx"
      :class="`cache-ctx--${usageInfo.contextTone}`"
    >{{ usageInfo.context }}</span>
    <template v-if="label">
      · {{ label.text }}
    </template>
    <span
      v-if="usageInfo.cost !== null"
      class="cache-cost"
      :title="costTitle"
    > · {{ usageInfo.cost }}</span>
  </p>
  <p
    v-else-if="label !== null && cache"
    class="cache-line"
    :class="`cache-line--${label.tone}`"
    :title="label.title"
  >
    {{ label.text }} · {{ contextLabel(cache.context_tokens) }}
  </p>
</template>
