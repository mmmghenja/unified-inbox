<script setup lang="ts">
// Лента сообщений: прокрутка вверх -> `next_before`; «прочитано» -> POST /channels/{id}/read до нижнего
// РЕАЛЬНО показанного сообщения (IntersectionObserver), с дебаунсом, только когда канал открыт руками и
// вкладка видима/в фокусе (спека, «Три колонки»). Для исходящих (`is_me`) и в фоне — не шлём.
//
// F6 (приёмка 6b): новое сообщение доскроллиевает вниз САМО, только если пользователь и так был у низа
// (~80px); если пролистал вверх читать историю — ленту не дёргаем. `loadOlderMessages` при быстром скролле
// защищена от параллельных вызовов на уровне стора (`Store.loadingOlder`).
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import type { Message } from '../api/types';
import AlbumRow from './AlbumRow.vue';
import MessageRow from './MessageRow.vue';
import { store } from '../store/instance';
import { buildFeedRows, type FeedRow } from '../store/feedRows';
import { createReadTracker } from '../store/readTracker';
import { createStickToBottom, NEAR_BOTTOM_PX } from '../util/stickBottom';

// Ф41-6б: `readEnabled` — беседа читается (отметка «прочитано» уходит), только пока он true. Столбец беседы на
// «Главной» передаёт true лишь когда указатель над ним или фокус внутри; обычная лента — всегда.
const props = withDefaults(defineProps<{ channelId: number; readEnabled?: boolean }>(), { readEnabled: true });
// Спека 2e: клик «Ответить» на сообщении НЕ-Slack-канала — режим ответа в поле ввода живёт у общего предка
// ленты и композера (ChannelView.vue), поэтому лента лишь прокидывает событие от строки наверх, ничего не
// решая сама (Slack-ветка решается прямо в MessageRow.vue — тред открывается напрямую через стор).
const emit = defineEmits<{ reply: [message: Message] }>();

const listEl = ref<HTMLElement | null>(null);
const messages = computed(() => store.messagesOf(props.channelId));
const loading = computed(() => store.state.loadingMessagesFor[props.channelId] ?? false);
// Спека 1d-1, п.2: подряд идущие сообщения одного отправителя с одинаковым `grouped_id` схлопываются в одну
// строку ленты (альбом) — чтение читать-трекинг (`findBottomVisibleMessage` ниже) при этом продолжает
// работать по ПЛОСКОМУ `messages`, DOM-строка альбома несёт `data-message-id` последнего (самого нового по
// порядку показа) сообщения группы — этого достаточно, чтобы курсор «прочитано» продвинулся за всю группу.
function feedRowKey(row: FeedRow): string {
  return row.kind === 'single' ? `m-${row.message.id}` : `a-${row.groupedId}-${row.messages[0]!.id}`;
}
const feedRows = computed(() => buildFeedRows(messages.value));

// Ф23-1: держаться низа (ResizeObserver на содержимом), пока владелец сам не прокрутил вверх; наша прокрутка
// (к низу, компенсация догрузки, к цели фокуса) `stick` не снимает — `util/stickBottom.ts`.
const sticky = createStickToBottom(() => listEl.value);
const isNearBottom = sticky.isNearBottom;

// Ф8-1 B: «показано» — ≥ 60 % высоты сообщения ИЛИ ≥ 60 % высоты ленты (длинное письмо); лента у низа —
// последнее сообщение показано; возврат фокуса/вкладки перезапускает отправку. Общая логика с панелью треда —
// `store/readTracker.ts`.
const readTracker = createReadTracker({
  messages: () => messages.value,
  isAtBottom: isNearBottom,
  markRead: (message) => {
    if (!props.readEnabled) return false;
    void store.markRead(props.channelId, message.id);
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
  if (listEl.value === null) return;
  sticky.onScroll();
  if (listEl.value.scrollTop < NEAR_BOTTOM_PX) {
    const prevHeight = listEl.value.scrollHeight;
    void store.loadOlderMessages(props.channelId).then(async () => {
      await nextTick();
      const el = listEl.value;
      if (el) sticky.programmatic(() => (el.scrollTop += el.scrollHeight - prevHeight));
    });
  }
}

watch(
  () => messages.value.length,
  async (newLen, oldLen) => {
    const shouldStick = sticky.stick;
    await nextTick();
    attachObserver();
    if (shouldStick && newLen > (oldLen ?? 0)) {
      await scrollToBottomNow();
    }
  },
);

watch(
  () => props.channelId,
  () => {
    readTracker.reset();
    sticky.setStick(true);
    void scrollToBottomNow().then(attachObserver).then(applyThreadJump);
  },
);

// Ф8-6 C1: переход с главной — прокрутить к сообщению («Нужна реакция») или к строке черновика («Открыть
// беседу», сообщение ищется по `outbox_id`) и подсветить. Цель ещё не загружена — ждём следующей пачки
// сообщений; нашли — снимаем цель со стора (повторный клик на главной заводит её заново).
const FOCUS_FLASH_MS = 3000;

function focusTargetId(): number | null {
  const focus = store.state.focus;
  if (focus === null || focus.channelId !== props.channelId) return null;
  if (focus.messageId !== null) return focus.messageId;
  if (focus.outboxId !== null) {
    return messages.value.find((m) => m.outbox_id === focus.outboxId)?.id ?? null;
  }
  return null;
}

function isInLastScreen(el: HTMLElement): boolean {
  const list = listEl.value;
  if (list === null) return false;
  const top = el.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;
  return list.scrollHeight - top < list.clientHeight;
}

async function applyFocus(): Promise<void> {
  const targetId = focusTargetId();
  if (targetId === null) {
    // цель-черновик без строки в ленте (он закреплён над полем): фокус не держим, остаёмся внизу
    const focus = store.state.focus;
    if (focus !== null && focus.channelId === props.channelId && focus.messageId === null && messages.value.length > 0) {
      store.clearFocus(props.channelId);
    }
    return;
  }
  await nextTick();
  await nextTick(); // после прокрутки к низу при открытии беседы
  const el = listEl.value?.querySelector(`[data-message-id="${targetId}"]`);
  if (!(el instanceof HTMLElement)) return;
  if (isInLastScreen(el)) {
    // цель в последнем экране (строка черновика — самая нижняя): низ держим, как при обычном открытии
    sticky.setStick(true);
    sticky.scrollToBottom();
  } else {
    sticky.setStick(false);
    sticky.programmatic(() => el.scrollIntoView?.({ block: 'center' }));
    sticky.anchor(el); // дорастание картинок выше цели не уводит её из вида
  }
  el.classList.add('message-row--focus');
  setTimeout(() => el.classList.remove('message-row--focus'), FOCUS_FLASH_MS);
  threadJumpPending = false; // приёмка Ф14-7: цель с главной показана — к родителю треда уже не прыгаем
  store.clearFocus(props.channelId);
}

watch(
  () => [store.state.focus?.seq, messages.value.length, props.channelId],
  () => void applyFocus(),
);

// Ф14-7 B2: беседа открыта, а непрочитанное только в тредах (`unread_top == 0`, `unread_threads > 0`) —
// прокрутить к родителю самого раннего треда с новыми ответами и подсветить (как переход с главной). Решается
// один раз на открытие, на первой непустой пачке; родителя в загруженной странице нет — лента остаётся внизу
// (кнопка «Треды: N» в шапке). Цель фокуса с главной важнее.
const THREAD_JUMP_FLASH_MS = 2000;
let threadJumpPending = true;

async function applyThreadJump(): Promise<void> {
  if (!threadJumpPending || messages.value.length === 0) return;
  threadJumpPending = false;
  // Приёмка Ф14-7: с главной цель фокуса — последнее сообщение карточки, у беседы с новым только в тредах это
  // ответ в треде, его в ленте нет — тогда цель не мешает прокрутке к родителю.
  const focusId = focusTargetId();
  if (focusId !== null && messages.value.some((m) => m.id === focusId)) return;
  const ch = store.state.sync.channelsById[props.channelId];
  if (ch === undefined || (ch.unread_top ?? 0) !== 0) return;
  // Ф66-5: свежий тред (в том числе неподписанный) — родитель по `fresh_thread_ext`; иначе как в Ф14-7.
  const parent =
    (ch.unread_threads ?? 0) > 0
      ? messages.value.find((m) => m.thread?.subscribed === true && m.thread.unread > 0)
      : messages.value.find((m) => ch.fresh_thread_ext != null && m.ext_id === ch.fresh_thread_ext);
  if (parent === undefined) return;
  await nextTick();
  await nextTick(); // после прокрутки к низу при открытии беседы
  const el = listEl.value?.querySelector(`[data-message-id="${parent.id}"]`);
  if (!(el instanceof HTMLElement)) return;
  sticky.setStick(false);
  sticky.programmatic(() => el.scrollIntoView?.({ block: 'center' }));
  sticky.anchor(el);
  el.classList.add('message-row--focus');
  setTimeout(() => el.classList.remove('message-row--focus'), THREAD_JUMP_FLASH_MS);
}

watch(
  () => props.channelId,
  () => {
    threadJumpPending = true;
  },
  { flush: 'sync' },
);

watch(
  () => messages.value.length,
  () => void applyThreadJump(),
);

// Беседа стала активной (указатель/фокус) — прочитанность перезапускается: сообщения, что пришли, пока она
// стояла на доске, отмечаются теперь.
watch(
  () => props.readEnabled,
  (enabled) => {
    if (enabled) readTracker.schedule();
  },
);

onMounted(() => {
  void scrollToBottomNow()
    .then(attachObserver)
    .then(applyFocus)
    .then(applyThreadJump);
  listEl.value?.addEventListener('scroll', onScroll);
  sticky.observe(listEl.value, listEl.value?.querySelector('ul') ?? null); // и сам контейнер: карточка черновика/поле ввода сжимают его
});

onBeforeUnmount(() => {
  readTracker.dispose();
  sticky.dispose();
  listEl.value?.removeEventListener('scroll', onScroll);
});
</script>

<template>
  <div
    ref="listEl"
    class="message-feed"
    role="log"
    aria-label="Лента сообщений"
  >
    <p
      v-if="loading"
      class="loading-hint"
    >
      Загрузка…
    </p>
    <ul>
      <template
        v-for="row in feedRows"
        :key="feedRowKey(row)"
      >
        <MessageRow
          v-if="row.kind === 'single'"
          :message="row.message"
          @reply="emit('reply', $event)"
        />
        <AlbumRow
          v-else
          :messages="row.messages"
        />
      </template>
    </ul>
  </div>
</template>
