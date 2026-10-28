<script setup lang="ts">
// Ф37-1 A4 (`docs/architecture.md`): «Без подтверждения ▾» в шапке беседы — какие роли сессий
// проекта могут отправлять сюда от имени владельца сразу. В списке: роли живых подписок проекта и уже разрешённые роли,
// каждая галочкой. Включение — только через подтверждение с фразой (роль не доказывает сессию: разрешение
// действует для ЛЮБОЙ сессии проекта с этой ролью); выключение — сразу. Отказ ядра (422) — текстом рядом.
// Беседе без проекта, в архиве, заготовке и `no_reply` контрола нет. Роли — машинный ввод: только текстом.
import { computed, ref, watch } from 'vue';

import { store } from '../store/instance';
import { usePopup } from '../util/popup';

const props = defineProps<{ channelId: number }>();

const channel = computed(() => store.state.sync.channelsById[props.channelId] ?? null);
const visible = computed(() => {
  const c = channel.value;
  if (c === null) return false;
  return (
    c.project_id !== null && c.project_id > 0 && c.archived !== true && !c.new_to && c.can_reply !== false
  );
});

const allowed = computed(() => channel.value?.autosend ?? []);
const roles = computed(() => {
  const pid = channel.value?.project_id ?? null;
  const list: string[] = [];
  for (const sub of store.state.peerMsgs.agentSubs) {
    if (sub.project_id === pid && sub.role && sub.live && !list.includes(sub.role)) list.push(sub.role);
  }
  for (const role of allowed.value) if (!list.includes(role)) list.push(role);
  return list;
});

const open = ref(false);
const btnEl = ref<HTMLElement | null>(null);
const menuEl = ref<HTMLElement | null>(null);
// Ф80-1 A2: общий механизм меню — Esc, нажатие мимо, одно открытое меню на приложение.
usePopup({ open, button: () => btnEl.value, panel: () => menuEl.value });
const error = ref<string | null>(null);
const busy = ref(false);

function toggleMenu(): void {
  open.value = !open.value;
  if (open.value) void store.refreshAgentSubs();
}

/** Фраза подтверждения включения (П2: «любая сессия с этой ролью»). */
function confirmText(role: string): string {
  return `Любая ваша сессия в проекте, подписанная ролью «${role}», будет отправлять в эту беседу от вашего имени сразу, без подтверждения, и нажимать кнопки ботов. Продолжить?`;
}

async function onToggle(role: string, event: Event): Promise<void> {
  const box = event.target as HTMLInputElement;
  const on = box.checked;
  if (busy.value) {
    box.checked = !on;
    return;
  }
  if (on && !window.confirm(confirmText(role))) {
    box.checked = false;
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    await store.setChannelAutosend(props.channelId, role, on);
  } catch (err) {
    box.checked = !on;
    error.value = err instanceof Error && err.message ? err.message : 'Не удалось изменить.';
  } finally {
    busy.value = false;
  }
}

watch(
  () => props.channelId,
  () => {
    open.value = false;
    error.value = null;
  },
);
</script>

<template>
  <span
    v-if="visible"
    class="autosend-control"
  >
    <button
      ref="btnEl"
      type="button"
      class="autosend-button"
      :aria-expanded="open"
      title="Роли сессий, которым можно отправлять сюда сразу и нажимать кнопки бота"
      @click="toggleMenu"
    >
      Без подтверждения ▾
    </button>
    <span
      v-if="open"
      ref="menuEl"
      class="autosend-menu"
    >
      <span
        v-if="roles.length === 0"
        class="autosend-empty"
      >нет сессий с ролью в проекте</span>
      <label
        v-for="role in roles"
        :key="role"
        class="autosend-item"
      >
        <input
          type="checkbox"
          :checked="allowed.includes(role)"
          :disabled="busy"
          @change="onToggle(role, $event)"
        >
        {{ role }}
      </label>
    </span>
    <span
      v-if="error !== null"
      class="map-error"
      role="alert"
    >{{ error }}</span>
  </span>
</template>
