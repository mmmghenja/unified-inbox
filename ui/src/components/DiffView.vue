<script setup lang="ts">
// Ф41-3а: unified diff — «+» зелёным, «−» красным, `@@` серым, моноширинно. Высота ограничена, прокрутка своя по
// обеим осям (на телефоне блок не раздвигает страницу). Текст — только текстовые узлы.
import { computed } from 'vue';

import { parseDiff } from '../util/sessionSteps';

const props = defineProps<{ patch: string; cut?: boolean }>();

const lines = computed(() => parseDiff(props.patch));
</script>

<template>
  <div class="diff-view">
    <pre class="diff-pre"><span
      v-for="(line, i) in lines"
      :key="i"
      class="diff-line"
      :class="`diff-line--${line.kind}`"
    >{{ line.text }}
</span></pre>
    <p
      v-if="cut"
      class="diff-cut"
    >
      diff обрезан
    </p>
  </div>
</template>
