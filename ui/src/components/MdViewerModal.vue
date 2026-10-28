<script setup lang="ts">
// Ф72-1 B: окно просмотра MD-файла поверх всего (на телефоне — на весь экран). Один хозяин в `MainApp`
// (`store/mdViewer.ts`). Ссылки на MD внутри файла открываются в том же окне, «‹ назад» возвращает.
// Закрывается по ✕, Esc и клику мимо.
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import { mdBack, mdClose, mdReload, mdViewer } from '../store/mdViewer';
import { copyText } from '../util/copy';
import ModelText from './ModelText.vue';

const dialog = ref<HTMLElement | null>(null);
const copied = ref('');

const top = computed(() => mdViewer.stack[mdViewer.stack.length - 1] ?? null);
const shownPath = computed(() => top.value?.path ?? top.value?.request ?? '');
const title = computed(() => shownPath.value.split('/').filter((s) => s !== '').pop() ?? shownPath.value);

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || top.value === null) return;
  event.stopPropagation();
  mdClose();
}

async function copyPath(): Promise<void> {
  const ok = await copyText(shownPath.value);
  copied.value = ok ? 'скопировано' : 'не вышло';
  setTimeout(() => (copied.value = ''), 2000);
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => dialog.value?.focus());
});
onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown));
</script>

<template>
  <Teleport to="body">
    <div
      v-if="top !== null"
      class="channel-modal-overlay md-viewer-overlay"
      @click.self="mdClose()"
    >
      <div
        ref="dialog"
        class="channel-modal md-viewer"
        role="dialog"
        aria-modal="true"
        :aria-label="`Файл: ${title}`"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <button
            v-if="mdViewer.stack.length > 1"
            type="button"
            class="link-button md-viewer-back"
            @click="mdBack()"
          >
            ‹ назад
          </button>
          <b class="channel-modal-title">{{ title }}</b>
          <span class="md-viewer-path">{{ shownPath }}</span>
          <span class="board-col-spacer" />
          <button
            type="button"
            class="link-button md-viewer-copy"
            @click="copyPath()"
          >
            {{ copied || 'копировать путь' }}
          </button>
          <button
            type="button"
            class="link-button md-viewer-reload"
            @click="mdReload()"
          >
            обновить
          </button>
          <button
            type="button"
            class="channel-modal-close"
            aria-label="Закрыть"
            @click="mdClose()"
          >
            ×
          </button>
        </header>
        <div class="md-viewer-body">
          <p
            v-if="top.status === 'loading'"
            class="md-viewer-loading"
          >
            загружаю…
          </p>
          <template v-else-if="top.status === 'not_found'">
            <p
              class="work-error"
              role="alert"
            >
              файл не найден: {{ top.request }}
            </p>
            <ul
              v-if="top.tried.length > 0"
              class="md-viewer-tried"
            >
              <li
                v-for="t in top.tried"
                :key="t"
              >
                {{ t }}
              </li>
            </ul>
          </template>
          <p
            v-else-if="top.status === 'too_big'"
            class="work-error"
            role="alert"
          >
            файл больше 2 МиБ
          </p>
          <p
            v-else-if="top.status === 'bad_path'"
            class="work-error"
            role="alert"
          >
            не похоже на путь к MD-файлу: {{ top.request }}
          </p>
          <p
            v-else-if="top.status === 'error'"
            class="work-error"
            role="alert"
          >
            {{ top.message }}
          </p>
          <ModelText
            v-else
            :key="top.path ?? top.request"
            :text="top.text"
            :from="top.path ?? undefined"
            :session-id="top.ctx.sessionId"
            :project-id="top.ctx.projectId"
          />
        </div>
      </div>
    </div>
  </Teleport>
</template>
