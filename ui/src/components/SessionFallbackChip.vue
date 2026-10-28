<script setup lang="ts">
// Ф83-3: чип «на запасном» в шапке сессии (столбец доски, окно, телефон). Подсказка: «основной — …, причина, с
// ЧЧ:ММ» (текст готовит ядро). Нет пометки — ничего не рисуется.
import { computed } from 'vue';

import type { HubSession } from '../api/types';
import { sessionFallbackTitle } from '../util/engineFallback';

const props = defineProps<{ session: Pick<HubSession, 'on_fallback' | 'on_fallback_text'> }>();
const title = computed(() => sessionFallbackTitle(props.session));
</script>

<template>
  <span
    v-if="session.on_fallback"
    class="session-fallback-chip"
    :title="title"
  >на запасном</span>
</template>
