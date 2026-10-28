<script setup lang="ts">
// Спека 1d-1, п.4: строка под сообщением из `reactions_json` — «👍 3 ❤️ 1», своя реакция (`me`) выделена.
// Ф10-3 (раздел D, П3, П11): там, где владелец может ставить реакции (`clickable`), чип — кнопка: клик ставит или
// снимает эту реакцию (плюсануть чужую / снять свою); свой эмодзи Telegram (`custom`) — не кнопка. Чип с
// операцией в пути — «ставится» (`pending`).
// Ф23-2: у каждого чипа со списком поставивших (`by`/`byMore`/`me`) — всплывашка со СПИСКОМ людей: наведение
// мыши или фокус — показать, уход — убрать; на телефоне долгое нажатие (500 мс) — та же всплывашка (следующий
// короткий клик после неё не ставит и не снимает реакцию), короткое — как раньше.
import { onBeforeUnmount, onMounted, ref } from 'vue';

import type { ParsedReaction } from '../render/reactions';

type ChipReaction = ParsedReaction & { pending?: boolean };

withDefaults(defineProps<{ reactions: ChipReaction[]; clickable?: boolean }>(), { clickable: false });
const emit = defineEmits<{ toggle: [raw: string] }>();

interface WhoLine {
  text: string;
  me: boolean;
}

// Снимаемая последняя реакция (ожидаемый счётчик 0) — без числа, пока источник не подтвердил.
function chipText(r: ChipReaction): string {
  return r.count > 0 ? `${r.label} ${r.count}` : r.label;
}

/** Строки всплывашки: «ты» первой (если своя), затем имена по порядку источника. */
function whoLines(r: ChipReaction): WhoLine[] {
  const lines: WhoLine[] = [];
  if (r.me) lines.push({ text: 'ты', me: true });
  for (const name of r.by ?? []) lines.push({ text: name, me: false });
  return lines;
}

function hasWho(r: ChipReaction): boolean {
  return r.me || (r.by?.length ?? 0) > 0 || (r.byMore ?? 0) > 0;
}

const LONG_PRESS_MS = 500;
const shown = ref<number | null>(null); // индекс чипа с открытой всплывашкой
let pressTimer: ReturnType<typeof setTimeout> | null = null;
let suppressClick = false; // долгое нажатие открыло всплывашку — клик после него не трогает реакцию

function clearPress(): void {
  if (pressTimer !== null) clearTimeout(pressTimer);
  pressTimer = null;
}

function onEnter(idx: number, e: PointerEvent): void {
  if (e.pointerType === 'touch') return; // на телефоне — только долгое нажатие
  shown.value = idx;
}

function onLeave(idx: number): void {
  if (shown.value === idx) shown.value = null;
}

function onPointerDown(idx: number, e: PointerEvent): void {
  if (e.pointerType !== 'touch') return;
  clearPress();
  suppressClick = false;
  pressTimer = setTimeout(() => {
    pressTimer = null;
    suppressClick = true;
    shown.value = idx;
  }, LONG_PRESS_MS);
}

function onPointerEnd(): void {
  clearPress();
}

function onClick(r: ChipReaction): void {
  if (suppressClick) {
    suppressClick = false;
    return;
  }
  shown.value = null;
  emit('toggle', r.raw);
}

function onContextMenu(e: Event): void {
  // долгое нажатие на телефоне открывает системное меню — нам оно не нужно
  if (suppressClick || pressTimer !== null) e.preventDefault();
}

function closeWho(): void {
  shown.value = null;
}

// Всплывашка, открытая долгим нажатием, закрывается нажатием в другое место.
const rootEl = ref<HTMLElement | null>(null);
function onOutsidePointerDown(e: Event): void {
  if (shown.value !== null && !(e.target instanceof Node && rootEl.value?.contains(e.target))) shown.value = null;
}
onMounted(() => document.addEventListener('pointerdown', onOutsidePointerDown));
onBeforeUnmount(() => {
  clearPress();
  document.removeEventListener('pointerdown', onOutsidePointerDown);
});
</script>

<template>
  <p
    v-if="reactions.length > 0"
    ref="rootEl"
    class="reactions"
  >
    <span
      v-for="(r, idx) in reactions"
      :key="idx"
      class="reaction-wrap"
      @pointerenter="onEnter(idx, $event)"
      @pointerleave="onLeave(idx)"
      @focusin="shown = idx"
      @focusout="onLeave(idx)"
      @keydown.esc="closeWho"
    >
      <button
        v-if="clickable && !r.custom"
        type="button"
        class="reaction reaction--button"
        :class="{ mine: r.me, pending: r.pending }"
        :title="r.pending ? 'ставится…' : hasWho(r) ? undefined : r.me ? 'снять свою реакцию' : 'поставить эту реакцию'"
        :aria-pressed="r.me"
        @pointerdown="onPointerDown(idx, $event)"
        @pointerup="onPointerEnd"
        @pointercancel="onPointerEnd"
        @contextmenu="onContextMenu"
        @click="onClick(r)"
      >
        {{ chipText(r) }}
      </button>
      <span
        v-else
        class="reaction"
        :class="{ mine: r.me, pending: r.pending }"
        :title="r.pending ? 'ставится…' : undefined"
        @pointerdown="onPointerDown(idx, $event)"
        @pointerup="onPointerEnd"
        @pointercancel="onPointerEnd"
        @contextmenu="onContextMenu"
      >{{ chipText(r) }}</span>
      <ul
        v-if="shown === idx && hasWho(r)"
        class="reaction-who"
        role="tooltip"
      >
        <li
          v-for="(line, i) in whoLines(r)"
          :key="i"
          :class="{ 'reaction-who__me': line.me }"
        >
          {{ line.text }}
        </li>
        <li
          v-if="(r.byMore ?? 0) > 0"
          class="reaction-who__more"
        >
          и ещё {{ r.byMore }} без имени
        </li>
      </ul>
    </span>
  </p>
</template>
