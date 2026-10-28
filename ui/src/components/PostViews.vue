<script setup lang="ts">
// Ф23-3: «👁 1 234» под постом канала (рядом с реакциями), подсказка — время снятия и пересылки. Нет `views` —
// ничего не рисуется.
import { computed } from 'vue';

import { parseViews, viewsLabel, viewsTitle } from '../render/views';

const props = defineProps<{ views: unknown }>();

const parsed = computed(() => parseViews(props.views));
</script>

<template>
  <span
    v-if="parsed !== null"
    class="post-views"
    :title="viewsTitle(parsed)"
    :aria-label="viewsTitle(parsed)"
  >{{ viewsLabel(parsed) }}</span>
</template>
