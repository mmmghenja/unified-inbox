<script setup lang="ts">
// Ф90-1: шапка окна сессии на телефоне и в окне «Написать…» — одна строка: [‹] точка состояния (подпись — по
// тапу/наведению) · имя (одной строкой, с многоточием) · чип «авто» (тап — вернуть ручной) · «⋯». Проект, модель,
// машина, подписка и прочее — в «Сведениях» меню (`SessionMenuItems`).
import { computed, ref } from 'vue';

import type { HubSession } from '../api/types';
import { store } from '../store/instance';
import type { SessionWindowHandle } from '../util/sessionHandle';
import { sessionHeadState } from '../util/sessionHead';
import { sessionName } from '../util/sessions';
import { usePopup } from '../util/popup';
import SessionMenuItems from './SessionMenuItems.vue';

const props = defineProps<{ session: HubSession; win: SessionWindowHandle | null; back?: boolean }>();
const emit = defineEmits<{ back: [] }>();

const head = computed(() =>
  sessionHeadState(props.session, store.state.sessions.pending[props.session.id]?.length ?? props.session.pending_requests),
);
const menuOpen = ref(false);
const menuButton = ref<HTMLElement | null>(null);
const menuPanel = ref<HTMLElement | null>(null);
// Ф80-1 A2: Esc, нажатие мимо, одно открытое меню; высота ограничена экраном (A3).
usePopup({ open: menuOpen, button: () => menuButton.value, panel: () => menuPanel.value });
</script>

<template>
  <header class="phone-session-head session-head-bar">
    <button
      v-if="back"
      type="button"
      class="back-button"
      aria-label="Назад к сессиям"
      @click="emit('back')"
    >
      ‹
    </button>
    <span
      class="board-dot session-head-dot"
      :class="`board-dot--${head.tone}`"
      role="img"
      :title="head.text"
      :aria-label="head.text"
    />
    <b class="phone-session-title">{{ sessionName(session) }}</b>
    <button
      v-if="session.mode === 'auto'"
      type="button"
      class="board-col-auto session-head-auto"
      title="Авто-режим: нажми, чтобы вернуть ручной"
      :disabled="win === null || win.busy"
      @click="win?.chooseMode('manual')"
    >
      авто
    </button>
    <button
      ref="menuButton"
      type="button"
      class="phone-session-menu-button"
      aria-label="Действия с сессией"
      :aria-expanded="menuOpen"
      @click="menuOpen = !menuOpen"
    >
      ⋯
    </button>
    <div
      v-if="menuOpen"
      ref="menuPanel"
      class="phone-session-menu"
      role="menu"
    >
      <SessionMenuItems
        :win="win"
        :session="session"
        @pick="menuOpen = false"
      />
    </div>
  </header>
</template>
