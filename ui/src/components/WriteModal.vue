<script setup lang="ts">
// Ф58-1 B: окно «Написать…» — одно окно поверх всего (образец — `OwnerFullModal`). Слева список адресатов («Мой
// агент», сессии хаба, внешние сессии, линии «не в сети», вне проектов; поиск), справа разговор. Транспорт по виду
// адресата: «Мой агент» — `OwnerTalk` (`/owner_channel/talk`); сессия хаба — её окно `SessionWindow` целиком (то же поле
// ввода, `say`); внешняя сессия, агент вне проектов, линия «не в сети» — `AgentDialog` (`peer-msgs`).
// Ф75-1: над разговором «Мой агент» — строка «Модель: … ▾» (`AgentModelPicker`, выбор модели агента владельца).
// Телефон: на весь экран, сначала список, тап — разговор с «‹» назад. Закрывается ×, Esc и кликом мимо.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { boardState, loadOfflineLines } from '../store/board';
import { useBoardGroups } from '../store/boardGroups';
import { store } from '../store/instance';
import { buildAgentLines, type AgentLine } from '../store/peerMsgs';
import { closeWrite, sameTarget, writeModal, type WriteTarget } from '../store/writeModal';
import { layout } from '../util/layout';
import { offlineName } from '../util/boardList';
import { sessionName } from '../util/sessions';
import { isShowcase } from '../util/showcase';
import { buildWriteTargets, filterWriteTargets, firstWriteTarget } from '../util/writeTargets';
import AgentDialog from './AgentDialog.vue';
import AgentModelPicker from './AgentModelPicker.vue';
import MachineChip from './MachineChip.vue';
import SessionHeadBar from './SessionHeadBar.vue';
import SessionWindow from './SessionWindow.vue';
import type { SessionWindowHandle } from '../util/sessionHandle';
import OwnerTalk from './OwnerTalk.vue';

const phone = computed(() => layout.mode === 'phone');
// Витрина: разговора «Мой агент» нет — ни строки в списке, ни окна разговора; окно открывается списком.
const showcase = isShowcase();
const query = ref('');
const dialog = ref<HTMLElement | null>(null);
const sessionWin = ref<SessionWindowHandle | null>(null);

const groups = useBoardGroups(() => undefined);
// Группы — те же, что в списке слева на доске (скрытые там проекты и убранные линии не показываются).
const agentLines = computed(() => buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs));
const agent = computed(() => store.state.owner_channel.agent);
const targets = computed(() =>
  buildWriteTargets({ groups: groups.value, agentLines: agentLines.value, agent: { online: agent.value.online, talk_unread: agent.value.talk_unread } }),
);
const shown = computed(() => filterWriteTargets(targets.value, query.value));

const target = computed(() => (showcase && writeModal.target?.kind === 'owner_channel' ? null : writeModal.target));
const session = computed(() => {
  const t = target.value;
  return t?.kind === 'session' ? (store.state.sessions.items[t.id] ?? null) : null;
});
const line = computed<AgentLine | null>(() => {
  const t = target.value;
  if (t?.kind !== 'agent') return null;
  const live = agentLines.value.find((l) => l.key === t.key);
  if (live !== undefined) return live;
  const o = boardState.offline.find((l) => l.key === t.key);
  if (o === undefined) return null;
  return {
    key: o.key,
    projectId: o.project_id,
    name: offlineName(o),
    hasRole: o.role !== null && o.role.trim() !== '',
    address: { project_id: o.project_id, role: o.role ?? '', number: o.role_no ?? 1 },
    sub: null,
    cwd: null,
    unread: 0,
  };
});
const title = computed(() => {
  const t = target.value;
  if (t === null) return 'Написать…';
  if (t.kind === 'owner_channel') return 'Мой агент';
  if (t.kind === 'session') return session.value === null ? 'Сессия' : sessionName(session.value);
  return line.value?.name ?? 'Агент';
});

function pick(next: WriteTarget): void {
  writeModal.target = next;
  if (next.kind !== 'owner_channel') writeModal.card = null;
}

function back(): void {
  writeModal.target = null;
}

function onSearchEnter(): void {
  const first = firstWriteTarget(targets.value, query.value);
  if (first !== null) pick(first);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  // Поверх окна открыто другое (полный текст карточки) — Esc закрывает его, не это окно.
  if (document.querySelector('.owner-full-modal') !== null) return;
  event.stopPropagation();
  closeWrite();
}

// Окно сессии хаба считается раскрытым, пока оно в окне «Написать»: стор дочитывает события и держит поток.
watch(
  () => session.value?.id ?? null,
  (id, old) => {
    if (old !== undefined && old !== null) store.state.sessions.expanded[old] = false;
    if (id !== null) {
      store.state.sessions.expanded[id] = true;
      void store.refreshSession(id);
    }
  },
  { immediate: true },
);

let stopOwner: (() => void) | null = null;
let stopSessions: (() => void) | null = null;
let stopAgents: (() => void) | null = null;

onMounted(() => {
  stopOwner = store.watchOwner();
  stopSessions = store.watchSessions();
  stopAgents = store.watchAgentSubs();
  void store.loadAgentSubs();
  void store.loadPeerMsgs();
  void loadOfflineLines();
  // Компьютер: сразу «Мой агент»; телефон — сначала список.
  if (showcase && writeModal.target?.kind === 'owner_channel') writeModal.target = null;
  if (writeModal.target === null && !phone.value && !showcase) writeModal.target = { kind: 'owner_channel' };
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => dialog.value?.focus());
});
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  stopOwner?.();
  stopSessions?.();
  stopAgents?.();
  const t = writeModal.target;
  if (t?.kind === 'session') store.state.sessions.expanded[t.id] = false;
});
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay write-overlay"
      :class="{ 'write-overlay--phone': phone }"
      @click.self="closeWrite"
    >
      <div
        ref="dialog"
        class="channel-modal write-modal"
        :class="{ 'write-modal--phone': phone }"
        role="dialog"
        aria-modal="true"
        aria-label="Написать"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <button
            v-if="phone && target !== null"
            type="button"
            class="back-button"
            aria-label="Назад к списку"
            data-test="write-back"
            @click="back"
          >
            ‹
          </button>
          <b class="channel-modal-title">{{ title }}</b>
          <span class="board-col-spacer" />
          <button
            type="button"
            class="channel-modal-close"
            aria-label="Закрыть"
            @click="closeWrite"
          >
            ×
          </button>
        </header>
        <div class="write-body">
          <nav
            v-if="!phone || target === null"
            class="write-list"
            aria-label="Кому написать"
          >
            <input
              v-model="query"
              type="search"
              class="write-search"
              placeholder="Найти: имя, роль, проект"
              aria-label="Найти адресата"
              data-test="write-search"
              @keydown.enter.prevent="onSearchEnter"
            >
            <button
              v-if="!showcase"
              type="button"
              class="board-row write-row write-row--owner"
              :class="{ 'write-row--active': sameTarget(target, shown.owner_channel.target) }"
              data-write="owner_channel"
              @click="pick(shown.owner_channel.target)"
            >
              <span
                class="board-dot"
                :class="`board-dot--${shown.owner_channel.dot}`"
              />
              <span class="board-row-name">{{ shown.owner_channel.name }}</span>
              <span class="board-row-sub">{{ shown.owner_channel.sub }}</span>
              <span
                v-if="shown.owner_channel.unread > 0"
                class="badge badge-react"
                data-test="write-unread"
              >{{ shown.owner_channel.unread }}</span>
            </button>
            <section
              v-for="group in shown.groups"
              :key="group.id"
              class="board-group"
              :data-project-id="group.id"
            >
              <header class="board-group-head">
                <span class="board-group-name">{{ group.name }}</span>
              </header>
              <button
                v-for="row in group.rows"
                :key="row.target.kind === 'session' ? `s${row.target.id}` : `a${row.target.kind === 'agent' ? row.target.key : ''}`"
                type="button"
                class="board-row write-row"
                :class="{ 'write-row--active': sameTarget(target, row.target), 'write-row--offline': row.offline }"
                :data-write="row.target.kind === 'session' ? `session:${row.target.id}` : `agent:${row.target.kind === 'agent' ? row.target.key : ''}`"
                @click="pick(row.target)"
              >
                <span
                  class="board-dot"
                  :class="`board-dot--${row.dot}`"
                />
                <span class="board-row-name">{{ row.name }}</span>
                <MachineChip :machine="row.machine" />
                <span
                  v-if="row.mode === 'auto'"
                  class="write-mode-auto"
                  data-test="write-mode"
                >авто</span>
                <span
                  v-else-if="row.mode === 'manual'"
                  class="board-row-sub"
                  data-test="write-mode"
                >ручной</span>
                <span class="board-row-sub">{{ row.sub }}</span>
                <span
                  v-if="row.unread > 0"
                  class="badge badge-react"
                  data-test="write-unread"
                >{{ row.unread }}</span>
              </button>
            </section>
            <p
              v-if="shown.groups.length === 0 && query.trim() !== ''"
              class="board-empty"
            >
              Никого не нашлось
            </p>
          </nav>
          <div
            v-if="!phone || target !== null"
            class="write-pane"
          >
            <template v-if="target !== null && target.kind === 'owner_channel' && !showcase">
              <AgentModelPicker />
              <OwnerTalk
                :card="writeModal.card"
                :branch="target.branch"
                @clear-card="writeModal.card = null"
              />
            </template>
            <template v-else-if="session !== null">
              <SessionHeadBar
                :session="session"
                :win="sessionWin"
              />
              <SessionWindow
                :key="session.id"
                ref="sessionWin"
                :session="session"
                column
              />
            </template>
            <AgentDialog
              v-else-if="line !== null"
              :key="line.key"
              :line="line"
            />
            <p
              v-else
              class="board-empty"
            >
              {{ target === null ? 'Выбери, кому написать' : 'Адресат ушёл или ещё не загрузился.' }}
            </p>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
