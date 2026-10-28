<script setup lang="ts">
// Ф44-1b: инлайн-кнопки бота Telegram под сообщением — ряды как в Telegram. Тексты и адреса — чужой ввод:
// только текстом (без разметки), ссылки — только http/https, `rel="noopener noreferrer"`. Нажатие `callback` —
// событие `press` с адресом (ряд и место с 1) и текстом; обработчик — в `MessageRow` (кусок 44-1c).
import { onBeforeUnmount, ref } from 'vue';

import type { ButtonPress, MessageButton, MessageButtons } from '../api/types';
import PressCard from './PressCard.vue';
import { copyText } from '../util/copy';
import type { OutcomeLine } from '../util/presses';

// Ф44-1d: `busy` — адреса (`ряд.место`, с 1) кнопок, чьё нажатие идёт; `waiting` — ждёт подтверждения владельца;
// `lines` — строки исхода под рядами (ответ бота — чужой текст, только текстом, ссылка — `<a>` без авто-открытия).
// `pending` — ждущие нажатия: клик по ждущей кнопке открывает под клавиатурой карточку «Нажать?» (решение там).
const props = withDefaults(
  defineProps<{
    buttons: MessageButtons;
    busy?: string[];
    waiting?: string[];
    lines?: OutcomeLine[];
    pending?: ButtonPress[];
  }>(),
  { busy: () => [], waiting: () => [], lines: () => [], pending: () => [] },
);
const openAddr = ref<string | null>(null);
function openCard(addr: string): void {
  openAddr.value = openAddr.value === addr ? null : addr;
}
function cardFor(addr: string): ButtonPress | undefined {
  return props.pending.find((p) => `${p.row}.${p.col}` === addr);
}
const emit = defineEmits<{ press: [payload: { row: number; col: number; text: string }] }>();

const PW_TITLE = 'требует пароль Telegram';
const OTHER_TITLE = 'только в приложении Telegram';
const COPIED_MS = 2000;

/** Ссылка кнопки, если это настоящий http(s)-адрес; иначе `null` (кнопка неактивна). */
function safeUrl(button: MessageButton): URL | null {
  if (button.k !== 'url' || typeof button.u !== 'string') return null;
  try {
    const url = new URL(button.u);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

function disabledTitle(button: MessageButton): string {
  if (button.k === 'callback') return PW_TITLE;
  return OTHER_TITLE;
}

const copiedKey = ref<string | null>(null);
let copiedTimer: ReturnType<typeof setTimeout> | null = null;

async function copy(key: string, button: MessageButton): Promise<void> {
  if (typeof button.c !== 'string') return;
  if (!(await copyText(button.c))) return;
  copiedKey.value = key;
  if (copiedTimer !== null) clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => {
    copiedKey.value = null;
  }, COPIED_MS);
}

onBeforeUnmount(() => {
  if (copiedTimer !== null) clearTimeout(copiedTimer);
});
</script>

<template>
  <div
    class="msg-buttons"
    data-testid="msg-buttons"
  >
    <div
      v-for="(row, r) in buttons.rows"
      :key="r"
      class="msg-buttons__row"
    >
      <template
        v-for="(button, c) in row"
        :key="c"
      >
        <button
          v-if="button.k === 'callback' && button.pw !== true"
          type="button"
          class="msg-btn"
          :disabled="busy.includes(`${r + 1}.${c + 1}`)"
          :title="waiting.includes(`${r + 1}.${c + 1}`) ? 'ждёт твоего решения' : button.t"
          @click="
            waiting.includes(`${r + 1}.${c + 1}`)
              ? openCard(`${r + 1}.${c + 1}`)
              : emit('press', { row: r + 1, col: c + 1, text: button.t })
          "
        >
          <span class="msg-btn__text">{{ button.t }}</span>
          <span
            v-if="busy.includes(`${r + 1}.${c + 1}`)"
            class="msg-btn__host"
          >нажимается…</span>
          <span
            v-else-if="waiting.includes(`${r + 1}.${c + 1}`)"
            class="msg-btn__host"
          >ждёт твоего решения</span>
        </button>
        <a
          v-else-if="safeUrl(button) !== null"
          class="msg-btn msg-btn--link"
          :href="safeUrl(button)!.href"
          target="_blank"
          rel="noopener noreferrer"
          :title="button.u"
        >
          <span class="msg-btn__text">{{ button.t }}</span>
          <span class="msg-btn__host">{{ safeUrl(button)!.hostname }}</span>
        </a>
        <button
          v-else-if="button.k === 'copy'"
          type="button"
          class="msg-btn msg-btn--copy"
          :title="button.c ? `Скопировать: ${button.c}` : 'Скопировать'"
          @click="copy(`${r}.${c}`, button)"
        >
          <span class="msg-btn__text">{{ button.t }}</span>
          <span class="msg-btn__host">{{ copiedKey === `${r}.${c}` ? 'скопировано' : 'скопировать' }}</span>
        </button>
        <button
          v-else
          type="button"
          class="msg-btn"
          disabled
          :title="disabledTitle(button)"
        >
          <span class="msg-btn__text">{{ button.t }}</span>
        </button>
      </template>
    </div>
    <PressCard
      v-if="openAddr !== null && cardFor(openAddr)"
      :press="cardFor(openAddr)!"
    />
    <p
      v-for="line in lines"
      :key="line.key"
      class="msg-buttons__line"
      :class="`msg-buttons__line--${line.kind}`"
      role="status"
    >
      <span>{{ line.text }}</span>
      <template v-if="line.url">
        {{ ' · ' }}
        <a
          :href="line.url"
          target="_blank"
          rel="noopener noreferrer"
          :title="line.url"
        >{{ line.host ?? line.url }}</a>
      </template>
    </p>
    <p
      v-if="buttons.cut"
      class="msg-buttons__cut"
    >
      часть кнопок не показана
    </p>
  </div>
</template>
