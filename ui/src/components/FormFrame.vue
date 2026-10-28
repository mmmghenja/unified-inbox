<script setup lang="ts">
// Ф80-1 C2: рамка формы сессии. `modal` — форма открывается окном поверх (столбец доски на компьютере: узкий столбец
// 380 px схлопывал ленту сессии); иначе — как прежде, внутри окна сессии (телефон, полноэкранное окно). Закрывают:
// Esc, нажатие на затемнение, «×».
import { onBeforeUnmount, onMounted } from 'vue';

import { openPopupCount } from '../util/popup';

defineProps<{ modal: boolean; title: string }>();
const emit = defineEmits<{ close: [] }>();

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && openPopupCount() === 0) emit('close');
}
// Ф80-2 п.7: и встроенная форма (телефон) закрывается Esc и «Закрыть» в своей шапке.
onMounted(() => {
  document.addEventListener('keydown', onKeydown);
});
onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown));
</script>

<template>
  <Teleport
    v-if="modal"
    to="body"
  >
    <div
      class="channel-modal-overlay form-frame-overlay"
      @click.self="emit('close')"
    >
      <div
        class="channel-modal role-text-modal form-frame-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">{{ title }}</b>
          <span class="board-col-spacer" />
          <button
            type="button"
            class="channel-modal-close"
            aria-label="Закрыть"
            @click="emit('close')"
          >
            ×
          </button>
        </header>
        <div class="role-text-body form-frame-body">
          <slot />
        </div>
      </div>
    </div>
  </Teleport>
  <section
    v-else
    class="form-frame-inline"
    :aria-label="title"
  >
    <header class="form-frame-inline-head">
      <b>{{ title }}</b>
      <span class="board-col-spacer" />
      <button
        type="button"
        class="form-frame-inline-close"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </header>
    <slot />
  </section>
</template>
