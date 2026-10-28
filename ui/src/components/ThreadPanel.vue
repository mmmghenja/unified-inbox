<script setup lang="ts">
// Панель треда (спека 2e, раздел «Панель треда»): четвёртая колонка справа от ленты, открыт один тред за
// раз (`store.state.openThread`), закрывается крестиком и Esc. Содержимое: родитель (или заглушка «исходное
// сообщение не загружено»), ответы страницами (прокрутка вверх — `loadOlderReplies`), поле ввода с `thread=`,
// заготовки и их статусы — как в ленте (тот же `MessageRow`/`OutboxStatusBadge`).
//
// «Прочитано» — та же логика, что `MessageFeed.vue` (нижнее РЕАЛЬНО показанное сообщение, дебаунс, только
// когда вкладка видима и в фокусе), но тредовой ручкой (`store.markThreadRead`), а не канальной.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { store } from '../store/instance';
import { threadKey } from '../store/threads';
import { createReadTracker } from '../store/readTracker';
import { createStickToBottom, NEAR_BOTTOM_PX } from '../util/stickBottom';
import { layout } from '../util/layout';
import Composer from './Composer.vue';
import MessageRow from './MessageRow.vue';
import NoReplyNotice from './NoReplyNotice.vue';

const open = computed(() => store.state.openThread);
const key = computed(() => (open.value !== null ? threadKey(open.value.channelId, open.value.threadExtId) : null));
const thread = computed(() => (key.value !== null ? store.state.sync.threads[key.value] : undefined));
const replies = computed(() => thread.value?.replies ?? []);
const parent = computed(() => thread.value?.parent ?? null);
const loading = computed(() => thread.value?.loading ?? false);
// Ф10-5 B5: в треде беседы трекера поля ответа тоже нет.
const channel = computed(() =>
  open.value !== null ? (store.state.sync.channelsById[open.value.channelId] ?? null) : null,
);
const canReply = computed(() => channel.value?.can_reply !== false);

const listEl = ref<HTMLElement | null>(null);

// Ф10-6 B3: `half` — панель поверх правой части ленты (клик по ленте её не закрывает), `phone` — на весь экран
// с «‹» (назад к ленте через историю телефона). Закрытие — × / «‹» и Esc.
const mode = computed(() => layout.mode);

function close(): void {
  store.goBack('thread');
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') close();
}

// Приёмка 2e (архитектор): как лента (`MessageFeed.vue`, F6) — при открытии и на новых ответах панель
// прокручена вниз, если пользователь и так был у низа; догрузка старых сохраняет позицию прокрутки.
// Ф23-1: и здесь низ держится наблюдением за высотой, пока владелец сам не прокрутил вверх (`util/stickBottom.ts`).
const sticky = createStickToBottom(() => listEl.value);
const isNearBottom = sticky.isNearBottom;

// --- «прочитано» — та же логика, что у ленты (Ф8-1 B4: общий `store/readTracker.ts`, не копия), тредовой
// ручкой. Родитель треда в отметку не входит — только ответы. ------------------------------------------------
const readTracker = createReadTracker({
  messages: () => replies.value,
  isAtBottom: isNearBottom,
  markRead: (message) => {
    if (open.value === null) return false;
    void store.markThreadRead(open.value.channelId, open.value.threadExtId, message.id);
    return true;
  },
});

function attachObserver(): void {
  readTracker.attach(listEl.value);
}

async function scrollToBottomNow(): Promise<void> {
  await nextTick();
  sticky.scrollToBottom();
}

function onScroll(): void {
  if (listEl.value === null || open.value === null) return;
  sticky.onScroll();
  if (listEl.value.scrollTop < NEAR_BOTTOM_PX) {
    const prevHeight = listEl.value.scrollHeight;
    void store.loadOlderReplies().then(async () => {
      await nextTick();
      const el = listEl.value;
      if (el) sticky.programmatic(() => (el.scrollTop += el.scrollHeight - prevHeight));
    });
  }
}

watch(
  () => replies.value.length,
  async (newLen, oldLen) => {
    const shouldStick = sticky.stick;
    await nextTick();
    attachObserver();
    if (shouldStick && newLen > (oldLen ?? 0)) await scrollToBottomNow();
  },
);

// Ф66-5: переход с «Нужна реакция» — цель фокуса внутри этого треда: прокрутить к сообщению и подсветить.
async function applyThreadFocus(): Promise<void> {
  const focus = store.state.focus;
  const cur = open.value;
  if (focus === null || cur === null || focus.messageId === null) return;
  if (focus.channelId !== cur.channelId || focus.threadExtId !== cur.threadExtId) return;
  await nextTick();
  await nextTick();
  const el = listEl.value?.querySelector(`[data-message-id="${focus.messageId}"]`);
  if (!(el instanceof HTMLElement)) return;
  sticky.setStick(false);
  sticky.programmatic(() => el.scrollIntoView?.({ block: 'center' }));
  sticky.anchor(el);
  el.classList.add('message-row--focus');
  setTimeout(() => el.classList.remove('message-row--focus'), 3000);
  store.clearFocus(cur.channelId);
}

watch(
  () => [store.state.focus?.seq, replies.value.length, key.value],
  () => void applyThreadFocus(),
);

watch(key, () => {
  readTracker.reset();
  sticky.setStick(true);
  void nextTick().then(attachObserver);
  void nextTick().then(observeContent);
  void scrollToBottomNow();
});

function observeContent(): void {
  sticky.observe(...(listEl.value ? [listEl.value] : []), ...Array.from(listEl.value?.querySelectorAll('ul') ?? []));
}

// Родитель треда — отдельный `<ul>` (`v-if`): появился после загрузки — наблюдаем и его.
watch(
  () => parent.value === null,
  () => void nextTick().then(observeContent),
);

onMounted(() => {
  window.addEventListener('keydown', onKeydown);
  void nextTick().then(attachObserver);
  void scrollToBottomNow();
  listEl.value?.addEventListener('scroll', onScroll);
  observeContent();
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown);
  readTracker.dispose();
  sticky.dispose();
  listEl.value?.removeEventListener('scroll', onScroll);
});
</script>

<template>
  <aside
    v-if="open"
    class="column column-thread"
    :class="{ 'column-thread--overlay': mode === 'half', 'column-thread--phone': mode === 'phone' }"
    aria-label="Тред"
  >
    <header class="thread-header">
      <button
        v-if="mode === 'phone'"
        type="button"
        class="back-button thread-back"
        aria-label="Назад к беседе"
        @click="close"
      >
        ‹
      </button>
      <strong>Тред</strong>
      <button
        v-if="mode !== 'phone'"
        type="button"
        class="thread-close"
        aria-label="Закрыть тред"
        @click="close"
      >
        ×
      </button>
    </header>
    <div
      ref="listEl"
      class="thread-messages"
    >
      <ul
        v-if="parent !== null"
        class="thread-parent"
      >
        <MessageRow :message="parent" />
      </ul>
      <p
        v-else
        class="thread-parent-missing"
      >
        исходное сообщение не загружено
      </p>
      <p
        v-if="loading"
        class="loading-hint"
      >
        Загрузка…
      </p>
      <ul>
        <MessageRow
          v-for="message in replies"
          :key="message.id"
          :message="message"
        />
      </ul>
    </div>
    <Composer
      v-if="open !== null && canReply"
      :channel-id="open.channelId"
      :thread="open.threadExtId"
    />
    <NoReplyNotice
      v-else-if="open !== null"
      :channel="channel"
    />
  </aside>
</template>
