<script setup lang="ts">
// Инструкция роли (только чтение) окном поверх. Текст — только как текст (переносы сохранены), без разбора
// разметки. Закрывается по ✕, Esc и клику мимо.
// Ф48-3: встроенных текстов ролей нет. С `sessionId` — общий текст хаба и файл роли сессии
// (`GET /sessions/{id}/role-text`: путь в проекте, содержимое или причина, почему не прочитан; файла нет —
// «Файл роли не задан»; Ф48-5: `pinned === false` — файл изменён после создания сессии Claude Code). С `roleKey` (формы, сессии ещё нет) — общий текст роли каталога (`GET /roles/{key}`).
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';

import * as api from '../api/client';
import type { SessionRoleText } from '../api/types';

const props = defineProps<{ roleKey?: string; sessionId?: number; title: string }>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLElement | null>(null);
const common = ref<string | null>(null);
const names = ref<string[]>([]);
const file = ref<SessionRoleText | null>(null);
const loading = ref(true);
const error = ref<string | null>(null);

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  event.stopPropagation();
  emit('close');
}

onMounted(async () => {
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => dialog.value?.focus());
  try {
    if (props.sessionId !== undefined) {
      const data = await api.getSessionRoleText(props.sessionId);
      common.value = data.common;
      file.value = data;
    } else {
      const data = await api.getRoleText(props.roleKey ?? '');
      common.value = data.text;
      names.value = data.names;
    }
  } catch (err) {
    const gone = err instanceof api.ApiError && err.status === 404;
    if (gone) error.value = props.sessionId !== undefined ? 'Такой сессии уже нет.' : 'Такой роли нет в каталоге.';
    else error.value = 'Не получилось загрузить инструкцию — попробуй ещё раз.';
  } finally {
    loading.value = false;
  }
});

onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown));
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay role-text-overlay"
      @click.self="emit('close')"
    >
      <div
        ref="dialog"
        class="channel-modal role-text-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="`Инструкция роли: ${title}`"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">{{ title }}</b>
          <span
            v-if="names.length > 0"
            class="role-text-names"
          >{{ names.join(', ') }}</span>
          <span class="board-col-spacer" />
          <button
            type="button"
            class="channel-modal-close"
            aria-label="Закрыть"
            @click="emit('close')"
          >
            ×
          </button>
        </header>
        <div class="role-text-body">
          <p
            v-if="loading"
            class="role-text-loading"
          >
            загружается…
          </p>
          <p
            v-else-if="error"
            class="work-error"
            role="alert"
          >
            {{ error }}
          </p>
          <template v-else-if="common !== null">
            <pre class="role-text-content">{{ common }}</pre>
            <template v-if="file">
              <p
                v-if="file.role_file"
                class="role-text-file"
              >
                Файл роли: <code class="role-text-file-path">{{ file.role_file }}</code>
              </p>
              <p
                v-else
                class="role-text-file role-text-file--none"
              >
                Файл роли не задан
              </p>
              <pre
                v-if="file.role_file && file.file_text !== null"
                class="role-text-content role-text-file-content"
              >{{ file.file_text }}</pre>
              <p
                v-else-if="file.role_file"
                class="work-error role-text-file-reason"
              >
                Файл не прочитан: {{ file.file_reason ?? 'причина неизвестна' }} — сессия запускается без него.
              </p>
              <p
                v-if="file.pinned === false"
                class="role-text-file-unpinned"
              >
                Файл изменён после создания сессии: сессия получает новую редакцию сообщением при каждом запуске. Закрепить её в системном тексте — «Новая с передачей дел».
              </p>
            </template>
          </template>
        </div>
      </div>
    </div>
  </Teleport>
</template>
