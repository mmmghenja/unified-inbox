<script setup lang="ts">
// Ф99-5: окно-шпаргалка горячих клавиш по «?». Список берётся из реестра (`util/hotkeys.ts`), своих клавиш не
// держит. Закрывается ✕, Esc, повторным «?» и кликом мимо; пока открыто, нижние записи реестра молчат.
import { computed, onBeforeUnmount, watch } from 'vue';

import { hotkeyHelp, listHotkeys, registerHotkeys } from '../util/hotkeys';

const keys = computed(() => (hotkeyHelp.open ? listHotkeys() : []));
const groups = computed(() => {
  const map = new Map<string, { label: string; desc: string }[]>();
  for (const key of keys.value) {
    const list = map.get(key.where) ?? [];
    list.push({ label: key.label, desc: key.desc });
    map.set(key.where, list);
  }
  return [...map.entries()];
});

function close(): void {
  hotkeyHelp.open = false;
}

let off: (() => void) | null = null;
watch(
  () => hotkeyHelp.open,
  (open) => {
    off?.();
    off = null;
    if (!open) return;
    off = registerHotkeys({
      modal: true,
      keys: [
        { code: 'Escape', label: 'Esc', where: 'шпаргалка', desc: 'закрыть', run: close },
        { code: 'Slash', shift: true, label: '?', where: 'шпаргалка', desc: 'закрыть', run: close },
      ],
    });
  },
);
onBeforeUnmount(() => off?.());
</script>

<template>
  <Teleport to="body">
    <div
      v-if="hotkeyHelp.open"
      class="channel-modal-overlay hotkeys-overlay"
      @click.self="close"
    >
      <div
        class="channel-modal hotkeys-help"
        role="dialog"
        aria-modal="true"
        aria-label="Горячие клавиши"
        data-test="hotkeys-help"
      >
        <header class="channel-modal-head">
          <strong>Горячие клавиши</strong>
          <button
            type="button"
            class="link-button"
            aria-label="Закрыть"
            @click="close"
          >
            ✕
          </button>
        </header>
        <section
          v-for="[where, items] in groups"
          :key="where"
          class="hotkeys-group"
        >
          <h4>{{ where }}</h4>
          <dl>
            <template
              v-for="item in items"
              :key="item.label"
            >
              <dt><kbd>{{ item.label }}</kbd></dt>
              <dd>{{ item.desc }}</dd>
            </template>
          </dl>
        </section>
        <p class="hotkeys-note">
          Не действуют в терминале и на телефоне. Одиночные клавиши — только вне полей ввода. «Разрешить» —
          Ctrl+Enter. Ctrl+Enter, Ctrl+Shift+Enter, Esc и ←/→ в черновике работают как раньше.
        </p>
      </div>
    </div>
  </Teleport>
</template>

<style>
.hotkeys-help { max-width: 480px; max-height: 80vh; overflow-y: auto; padding: 12px 16px; }
.hotkeys-group h4 { margin: 10px 0 4px; font-size: 0.85em; opacity: 0.7; }
.hotkeys-group dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 0; }
.hotkeys-group dt, .hotkeys-group dd { margin: 0; }
.hotkeys-note { font-size: 0.8em; opacity: 0.7; }
</style>
