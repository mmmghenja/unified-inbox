<script setup lang="ts">
// Ф41-3в: чипы приложенного к реплике сессии — имя, размер, «×»; ошибка загрузки — строкой у чипа; под файлом вида
// `file` — подсказка про путь в папке проекта. Имена файлов — чужой ввод: только текстом.
import type { SessionFileDraft } from '../api/types';
import { FILE_KIND_NOTE, formatBytes } from '../util/sessionSteps';

// `no-note` — без подсказки про путь в папке проекта (разговор с агентом: файл уходит ему, не сессии).
defineProps<{ files: SessionFileDraft[]; noNote?: boolean }>();
defineEmits<{ remove: [localId: number] }>();
</script>

<template>
  <ul
    v-if="files.length > 0"
    class="session-files"
    aria-label="Приложенные файлы"
  >
    <li
      v-for="item in files"
      :key="item.localId"
      class="session-file"
      :class="`session-file--${item.status}`"
    >
      <span class="session-file-name">{{ item.name }}</span>
      <span class="session-file-size">
        <template v-if="item.status === 'uploading'">загружается…</template>
        <template v-else>{{ formatBytes(item.size) }}</template>
      </span>
      <button
        type="button"
        class="session-file-remove"
        :aria-label="`Убрать ${item.name}`"
        @click="$emit('remove', item.localId)"
      >
        ×
      </button>
      <span
        v-if="item.status === 'error'"
        class="session-file-error"
        role="alert"
      >{{ item.error }}</span>
      <span
        v-else-if="item.kind === 'file' && !noNote"
        class="session-file-note"
      >{{ FILE_KIND_NOTE }}</span>
    </li>
  </ul>
</template>
