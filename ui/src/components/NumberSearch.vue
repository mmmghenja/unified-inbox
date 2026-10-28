<script setup lang="ts">
// Поле «№» над списком черновиков и очереди отправки: набранные цифры оставляют только строки с таким номером
// (`util/draftNumber.ts`). Состояние держит родитель (`v-model`).
const model = defineModel<string>({ required: true });
const props = withDefaults(defineProps<{ placeholder?: string; label?: string }>(), {
  placeholder: 'номер черновика',
  label: 'Найти по номеру черновика',
});
</script>

<template>
  <label class="number-search">
    <span class="number-search-label">№</span>
    <input
      v-model="model"
      type="text"
      inputmode="numeric"
      class="number-search-input"
      :placeholder="props.placeholder"
      :aria-label="props.label"
      autocomplete="off"
      maxlength="20"
    >
    <button
      v-if="model !== ''"
      type="button"
      class="link-button number-search-clear"
      aria-label="Сбросить поиск по номеру"
      @click="model = ''"
    >✕</button>
  </label>
</template>
