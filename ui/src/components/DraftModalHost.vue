<script setup lang="ts">
// Общий хозяин окна черновика при листании (`store/draftModal.ts`). Показывает черновик `draftModal.id` из списка
// главной. Следит за черновиком, открытым в любом окне (`viewing`): пока он в списке — помнит порядок черновиков
// его проекта; ушёл из списка (отправлен, отклонён) — открывает следующий по этому порядку, нет такого — ничего.
// Слежение — синхронное: строка и её окно исчезают вместе с черновиком раньше, чем успели бы сообщить сами.
import { computed, watch } from 'vue';

import { draftModal, draftSiblings, nextAfter } from '../store/draftModal';
import { store } from '../store/instance';
import DraftModal from './DraftModal.vue';

const item = computed(() =>
  draftModal.id === null ? null : (store.state.home.drafts.find((d) => d.id === draftModal.id) ?? null),
);

let order: number[] = [];
watch(
  () => [draftModal.viewing, store.state.home.drafts] as const,
  ([viewing, all]) => {
    if (viewing === null) return;
    const current = all.find((d) => d.id === viewing);
    if (current !== undefined) {
      order = draftSiblings(all, current).map((d) => d.id);
      return;
    }
    const next = order.includes(viewing) ? nextAfter(order, viewing, new Set(all.map((d) => d.id))) : null;
    order = [];
    draftModal.id = next;
    draftModal.viewing = next;
  },
  { immediate: true, flush: 'sync' },
);

function onClose(id: number): void {
  if (draftModal.id === id) draftModal.id = null;
}
</script>

<template>
  <DraftModal
    v-if="item !== null"
    :key="item.id"
    :item="item"
    hosted
    @close="onClose(item.id)"
  />
</template>
