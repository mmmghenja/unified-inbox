<script setup lang="ts">
// Тихий номер беседы (`channel.id`): так её называют сессии («беседа #20»). Не сжимается, не переносится.
defineProps<{ id: number }>();
</script>

<template>
  <span
    class="chan-no"
    :title="`Номер беседы #${id}`"
  >#{{ id }}</span>
</template>
