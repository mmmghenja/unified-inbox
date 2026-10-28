<script setup lang="ts">
// Ф34-1 A6: блок «Стадии» вкладки проекта — стадия -> число -> раскрывается списком бесед (клик открывает беседу).
// Показывается, только если в проекте есть хоть одна стадия.
import { ref } from 'vue';

import { store } from '../store/instance';
import { isPhone } from '../util/layout';
import type { StageGroup } from '../util/stages';

defineProps<{ groups: StageGroup[] }>();

const open = ref<Set<string>>(new Set());

// Ф99-6: беседа из «Стадий» на компьютере — окном поверх (как из «Ждёт тебя»); телефон — обычным переходом.
function openChannel(id: number): void {
  if (isPhone()) void store.openChannel(id);
  else void store.openChannelOverlay(id);
}

function toggle(key: string): void {
  const next = new Set(open.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  open.value = next;
}
</script>

<template>
  <section
    v-if="groups.length > 0"
    class="stages-block"
    aria-label="Стадии"
  >
    <h2 class="attention-group-title">
      Стадии
    </h2>
    <ul class="stages-list">
      <li
        v-for="group in groups"
        :key="group.key"
      >
        <button
          type="button"
          class="stages-row"
          :aria-expanded="open.has(group.key)"
          :data-stage-key="group.key"
          @click="toggle(group.key)"
        >
          <span>{{ open.has(group.key) ? '▾' : '▸' }} {{ group.label }}</span>
          <span class="badge badge-quiet">{{ group.count }}</span>
        </button>
        <ul
          v-if="open.has(group.key)"
          class="stages-channels"
        >
          <li
            v-for="channel in group.channels"
            :key="channel.id"
          >
            <button
              type="button"
              class="stages-channel"
              :title="channel.stage_note || undefined"
              :data-channel-id="channel.id"
              @click="openChannel(channel.id)"
            >
              <span class="channel-title">{{ channel.title ?? '(без названия)' }}</span>
              <span
                v-if="channel.stage_note"
                class="stages-note"
              >{{ channel.stage_note }}</span>
            </button>
          </li>
        </ul>
      </li>
    </ul>
  </section>
</template>
