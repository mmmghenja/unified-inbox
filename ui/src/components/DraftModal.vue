<script setup lang="ts">
// Ф20-4 C4 (`docs/architecture.md`): окно черновика — диалог поверх страницы. Внутри обычный `DraftCard`
// со всеми действиями (правка, отправка, отклонение, файлы, «Открыть беседу»). Закрывается по ✕, Esc и клику мимо,
// а после «Открыть беседу» — само (`done` карточки). Фокус при открытии уходит в окно;
// вернуть его на «›» — дело строки (`DraftRow`) по событию `close`.
// Владелец 12.10.2026: черновики проекта листаются («‹ 2 из 5 ›», клавиши ← → вне поля ввода); после отправки или
// отклонения открывается следующий черновик проекта, после последнего окно закрывается. Другой черновик
// показывает общий хозяин (`DraftModalHost`, `store/draftModal.ts`): строка исчезает вместе со своим черновиком.
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import type { OutboxListItem } from '../api/types';
import { draftModal, draftSiblings } from '../store/draftModal';
import { store } from '../store/instance';
import DraftCard from './DraftCard.vue';

const props = defineProps<{ item: OutboxListItem; hosted?: boolean }>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLElement | null>(null);

const siblings = computed(() => draftSiblings(store.state.home.drafts, props.item));
const index = computed(() => siblings.value.findIndex((d) => d.id === props.item.id));
function go(id: number): void {
  draftModal.id = id;
  draftModal.viewing = id;
  if (!props.hosted) emit('close');
}

function step(delta: number): void {
  const target = siblings.value[index.value + delta];
  if (target !== undefined) go(target.id);
}

/** Закрытие владельцем (✕, Esc, клик мимо, «Открыть беседу»): листание кончено. */
function close(): void {
  if (draftModal.viewing === props.item.id) draftModal.viewing = null;
  emit('close');
}

function onDone(): void {
  // Черновик остался в списке — это «Открыть беседу». Ушёл (отправлен, отклонён) — следующий черновик проекта
  // уже открыл общий хозяин (`DraftModalHost`), это окно просто закрывается.
  if (store.state.home.drafts.some((d) => d.id === props.item.id)) close();
  else emit('close');
}

function inField(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.stopPropagation();
    close();
  } else if ((event.key === 'ArrowRight' || event.key === 'ArrowLeft') && !inField(event.target)) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    step(event.key === 'ArrowRight' ? 1 : -1);
  }
}

onMounted(() => {
  draftModal.viewing = props.item.id;
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => dialog.value?.focus());
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  // Окно убрали вместе со строкой (фильтр, смена вида), а черновик на месте — листание кончено.
  if (draftModal.viewing === props.item.id && store.state.home.drafts.some((d) => d.id === props.item.id)) {
    draftModal.viewing = null;
  }
});
</script>

<template>
  <div
    class="draft-modal-overlay"
    @click.self="close"
  >
    <div
      ref="dialog"
      class="draft-modal"
      role="dialog"
      aria-modal="true"
      :aria-label="`Черновик #${item.id}`"
      tabindex="-1"
    >
      <button
        type="button"
        class="draft-modal-close"
        aria-label="Закрыть"
        @click="close"
      >
        ✕
      </button>
      <p
        v-if="siblings.length > 1"
        class="draft-modal-nav"
      >
        <button
          type="button"
          class="draft-modal-prev"
          aria-label="Предыдущий черновик проекта"
          :disabled="index <= 0"
          @click="step(-1)"
        >
          ‹
        </button>
        <span class="draft-modal-count">{{ index + 1 }} из {{ siblings.length }}{{ item.project_name ? ` · ${item.project_name}` : '' }}</span>
        <button
          type="button"
          class="draft-modal-next"
          aria-label="Следующий черновик проекта"
          :disabled="index >= siblings.length - 1"
          @click="step(1)"
        >
          ›
        </button>
      </p>
      <DraftCard
        :item="item"
        show-address
        @done="onDone"
      />
    </div>
  </div>
</template>
