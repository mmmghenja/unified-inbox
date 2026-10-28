<script setup lang="ts">
// Ф35-3: блок «Агенты» вкладки «Claude» — агенты в сети по проектам («вне проектов» — своей группой с папкой сессии),
// строка раскрывается в диалог «Владелец <-> агент». Роли, имена проектов и папки — чужой ввод: только текстом.
import { computed, reactive, ref, watch } from 'vue';

import { store } from '../store/instance';
import {
  buildAgentLines,
  engineLabel,
  listeningLabel,
  modeLabel,
  type AgentLine,
} from '../store/peerMsgs';
import { safeColor } from '../util/color';
import SessionCacheLine from './SessionCacheLine.vue';
import AgentDialog from './AgentDialog.vue';
import type { AgentDraft } from './agentDraft';

defineProps<{ nowMs?: number }>();

// Ф36-2 B2: открытый диалог привязан к подписке — ключ линии может смениться (роль на месте), `sub.id` остаётся.
const open = ref<string | null>(null);
const openSubId = ref<number | null>(null);
// Ф36-2 B3: черновик и ошибка — здесь, не в `AgentDialog`: строка линии перерисовывается и исчезает.
const draft = reactive<AgentDraft>({ text: '', error: null, busy: false });

const lines = computed(() => buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs));

interface Group {
  projectId: number;
  title: string;
  color: string | null;
  lines: AgentLine[];
}

const groups = computed<Group[]>(() => {
  const byProject = new Map<number, AgentLine[]>();
  for (const line of lines.value) {
    const list = byProject.get(line.projectId) ?? [];
    list.push(line);
    byProject.set(line.projectId, list);
  }
  const result: Group[] = [];
  for (const [projectId, list] of byProject) {
    const project = store.state.projects.find((p) => p.id === projectId);
    result.push({
      projectId,
      title: projectId === 0 ? 'Вне проектов' : (project?.name ?? `Проект ${projectId}`),
      color: project?.color ?? null,
      // Приёмка 35-3: агенты с ролью — по имени («маркетолог 1» раньше «маркетолог 2»), безрольные — после.
      lines: [...list].sort(
        (a, b) => Number(b.hasRole) - Number(a.hasRole) || a.name.localeCompare(b.name, 'ru', { numeric: true }),
      ),
    });
  }
  // Проекты — в порядке вкладок, «вне проектов» — последней.
  const order = (id: number): number => (id === 0 ? Number.MAX_SAFE_INTEGER : store.state.projects.findIndex((p) => p.id === id));
  return result.sort((a, b) => order(a.projectId) - order(b.projectId));
});

// Ф36-2 B2: ключ открытой линии исчез, а подписка жива под другим ключом — диалог остаётся на ней.
watch(lines, (list) => {
  if (open.value === null || list.some((l) => l.key === open.value)) return;
  const moved = openSubId.value === null ? undefined : list.find((l) => l.sub?.id === openSubId.value);
  if (moved !== undefined) open.value = moved.key;
});

function resetDraft(): void {
  draft.text = '';
  draft.error = null;
  draft.busy = false;
}

function toggle(line: AgentLine): void {
  if (open.value === line.key) {
    open.value = null;
    openSubId.value = null;
  } else {
    open.value = line.key;
    openSubId.value = line.sub?.id ?? null;
  }
  resetDraft();
}

// Ф36-2 B3: линия исчезла совсем, а в поле ещё текст или ошибка — показываем их, пока владелец не закроет.
const orphan = computed(
  () => open.value !== null && !lines.value.some((l) => l.key === open.value) && (draft.text !== '' || draft.error !== null),
);

function closeOrphan(): void {
  open.value = null;
  openSubId.value = null;
  resetDraft();
}
</script>

<template>
  <section
    class="agents-block"
    aria-label="Агенты"
  >
    <h2 class="agents-title">
      Агенты
    </h2>
    <p
      v-if="store.state.peerMsgs.error"
      class="home-error"
      role="alert"
    >
      {{ store.state.peerMsgs.error }}
    </p>
    <div
      v-if="orphan"
      class="agent-orphan"
      role="alert"
    >
      <p class="agent-hint">
        Агент ушёл из сети — вот что осталось в поле:
      </p>
      <p
        v-if="draft.text !== ''"
        class="agent-msg-text"
      >
        {{ draft.text }}
      </p>
      <p
        v-if="draft.error !== null"
        class="agent-error"
      >
        {{ draft.error }}
      </p>
      <button
        type="button"
        @click="closeOrphan"
      >
        Закрыть
      </button>
    </div>
    <p
      v-if="groups.length === 0"
      class="agents-empty"
    >
      Агентов в сети нет
    </p>
    <div
      v-for="group in groups"
      :key="group.projectId"
      class="agents-group"
      :data-project-id="group.projectId"
    >
      <h3 class="agents-group-name">
        <span
          v-if="group.projectId !== 0"
          class="color-dot"
          :style="{ backgroundColor: safeColor(group.color) }"
        />
        {{ group.title }}
      </h3>
      <ul class="agents-list">
        <li
          v-for="line in group.lines"
          :key="line.key"
          class="agent-item"
        >
          <button
            type="button"
            class="agent-row"
            :aria-expanded="open === line.key"
            @click="toggle(line)"
          >
            <span class="agent-name">{{ line.name }}</span>
            <template v-if="line.sub !== null">
              <span class="agent-meta">{{ engineLabel(line.sub.engine) }}</span>
              <span class="agent-meta">{{ listeningLabel(line.sub) }}</span>
              <span class="agent-meta">{{ modeLabel(line.sub) }}</span>
            </template>
            <span
              v-else
              class="agent-meta"
            >не в сети</span>
            <span
              v-if="line.cwd"
              class="agent-meta agent-cwd"
            >{{ line.cwd }}</span>
            <span
              v-if="line.unread > 0"
              class="badge badge-react agent-unread"
              :title="`Непрочитанных ответов: ${line.unread}`"
            >{{ line.unread }}</span>
          </button>
          <SessionCacheLine
            v-if="line.sub?.cache"
            :cache="line.sub.cache"
            :now-ms="nowMs"
          />
          <!-- Ф99-6: переписка — окном поверх (разовое чтение), а не вложенным блоком. -->
          <Teleport
            v-if="open === line.key"
            to="body"
          >
            <div
              class="channel-modal-overlay agent-dialog-overlay"
              @click.self="toggle(line)"
              @keydown.esc.stop="toggle(line)"
            >
              <div
                class="channel-modal agent-dialog-modal"
                role="dialog"
                aria-modal="true"
                :aria-label="`Переписка: ${line.name}`"
                tabindex="-1"
              >
                <header class="channel-modal-head">
                  <b class="channel-modal-title">{{ line.name }}</b>
                  <span class="board-col-spacer" />
                  <button
                    type="button"
                    class="channel-modal-close"
                    aria-label="Закрыть"
                    @click="toggle(line)"
                  >
                    ×
                  </button>
                </header>
                <AgentDialog
                  :line="line"
                  :draft="draft"
                />
              </div>
            </div>
          </Teleport>
        </li>
      </ul>
    </div>
  </section>
</template>
