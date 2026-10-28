<script setup lang="ts">
// Ф99-8: «Порядок проектов» — кнопки «↑» и «↓», сохраняется сразу (`PUT /projects/order`).
import { computed, ref } from 'vue';

import * as api from '../api/client';
import { store } from '../store/instance';
import { movedIds, orderedProjects } from '../util/projectOrder';

const busy = ref(false);
const error = ref<string | null>(null);
const list = computed(() => orderedProjects(store.state.projects));

async function move(id: number, delta: -1 | 1): Promise<void> {
  if (busy.value) return;
  const ids = list.value.map((p) => p.id);
  const next = movedIds(ids, id, delta);
  if (next.every((v, i) => v === ids[i])) return;
  busy.value = true;
  error.value = null;
  try {
    await api.putProjectOrder(next);
    await store.loadProjects();
  } catch (err) {
    error.value = err instanceof api.ApiError && err.message ? err.message : 'Не удалось сохранить порядок.';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section
    v-if="list.length > 1"
    class="access-section project-order"
  >
    <h2>Порядок проектов</h2>
    <ul class="mapping-project-list">
      <li
        v-for="(project, index) in list"
        :key="project.id"
        class="project-order-row"
        :data-project-id="project.id"
      >
        <span class="project-order-name">{{ project.name ?? `проект #${project.id}` }}</span>
        <button
          type="button"
          class="project-order-up"
          :aria-label="`Выше: ${project.name ?? project.id}`"
          :disabled="busy || index === 0"
          @click="move(project.id, -1)"
        >
          ↑
        </button>
        <button
          type="button"
          class="project-order-down"
          :aria-label="`Ниже: ${project.name ?? project.id}`"
          :disabled="busy || index === list.length - 1"
          @click="move(project.id, 1)"
        >
          ↓
        </button>
      </li>
    </ul>
    <p
      v-if="error !== null"
      class="access-error"
      role="alert"
    >
      {{ error }}
    </p>
  </section>
</template>
