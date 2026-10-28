<script setup lang="ts">
// Ф41-6: пункты меню «⋯» окна сессии — одни и те же в шапке столбца доски и в шапке окна на телефоне: «Усыпить» /
// «Поднять», «Сжать», «Модель и усилие…», «Новая с передачей дел…», «Открыть в терминале», «Настройка подписки»,
// «Закрыть». Подтверждения и формы — прежние, внутри окна (`SessionWindow`, через его `defineExpose`).
import { computed } from 'vue';

import type { HubSession } from '../api/types';
import { store } from '../store/instance';
import { agentStateParts, cardsNote, dayNote } from '../util/agentState';
import { isPhone } from '../util/layout';
import { sessionHeadState } from '../util/sessionHead';
import { sessionStatusParts } from '../util/sessionStatus';
import { subLine } from '../util/sessionSub';
import { modelChipText } from '../util/sessions';
import MachineChip from './MachineChip.vue';
import SessionBgChip from './SessionBgChip.vue';
import SessionFallbackChip from './SessionFallbackChip.vue';
import { engineProfile } from '../util/engineProfiles';
import type { SessionWindowHandle } from '../util/sessionHandle';

const props = defineProps<{ win: SessionWindowHandle | null; session: HubSession }>();
const emit = defineEmits<{ pick: [] }>();
// Ф74-1: движок сжимает разговор сам (`compacts_self` профиля) — «Сжать» и «Сжатие…» у него нет.
const compactsSelf = computed(() => engineProfile(props.session.engine)?.compacts_self === true);
// Ф74-8: вместо пропавших пунктов — причина строкой (владелец видит, почему «Сжать» нет).
const compactNote = computed(() => {
  const label = engineProfile(props.session.engine)?.label || 'Движок';
  return `${label} сжимает разговор сам — «Сжать» и «Сжатие…» у него нет`;
});

// Ф48-3: «Инструкция роли» есть у любой сессии — общий текст хаба и файл роли (или «Файл роли не задан»).
// Ф90-1: «Сведения» — то, что раньше жило чипами и строками в шапке и под полем ввода: проект, модель · усилие,
// запасной движок, машина, фоновые задачи, подпапка, состояние, кэш/контекст/$, подписка. Считается при открытии меню.
const info = computed(() => {
  const now = Date.now();
  const s = props.session;
  const head = sessionHeadState(s, store.state.sessions.pending[s.id]?.length ?? s.pending_requests);
  const project = store.state.projects.find((p) => p.id === s.project_id)?.name ?? null;
  const sub = subLine(s);
  const agent = agentStateParts(s, now);
  const day = dayNote(s, store.state.projects);
  if (day !== null && !agent.includes(day)) agent.unshift(day);
  const cards = cardsNote(s);
  return {
    state: head.text,
    project,
    model: modelChipText(s),
    status: sessionStatusParts(s, now).filter((p) => p !== s.model?.trim()).join(' · '),
    agent: [...(cards !== null ? [cards] : []), ...agent].join(' · '),
    sub: `${sub.text}${sub.state ? ` · ${sub.state}` : ''}`,
  };
});

function pick(action: (win: SessionWindowHandle) => unknown): void {
  const win = props.win;
  emit('pick');
  if (win !== null) void action(win);
}

</script>

<template>
  <div
    class="session-menu-info"
    role="note"
    data-test="session-info"
  >
    <p class="session-menu-info-line">
      <span class="session-menu-info-state">{{ info.state }}</span><template v-if="info.project">
        · {{ info.project }}
      </template>
    </p>
    <p class="session-menu-info-chips">
      <button
        type="button"
        class="session-model-chip"
        title="Модель и усилие"
        @click="pick((w) => w.openPanel('model'))"
      >
        {{ info.model }}
      </button>
      <SessionFallbackChip :session="session" />
      <SessionBgChip :session="session" />
      <MachineChip :machine="session.machine" />
    </p>
    <p
      v-if="info.status"
      class="session-menu-info-line"
    >
      {{ info.status }}
    </p>
    <p
      v-if="info.agent"
      class="session-menu-info-line"
    >
      {{ info.agent }}
    </p>
    <p class="session-menu-info-line session-menu-info-sub">
      {{ info.sub }}
    </p>
  </div>
  <div
    v-if="session.subdir"
    class="session-menu-subdir"
    role="note"
    :title="session.subdir"
  >
    Папка: {{ session.subdir }}
  </div>
  <button
    type="button"
    role="menuitemradio"
    class="session-menu-mode"
    :aria-checked="session.mode === 'auto'"
    :disabled="win === null || win.busy"
    @click="pick((w) => w.chooseMode(session.mode === 'auto' ? 'manual' : 'auto'))"
  >
    {{ session.mode === 'auto' ? 'Режим: авто — вернуть ручной' : 'Режим: ручной — включить авто' }}
  </button>
  <button
    v-if="session.state === 'asleep'"
    type="button"
    role="menuitem"
    class="session-menu-wake"
    :disabled="win === null || win.busy"
    @click="pick((w) => w.wake())"
  >
    Поднять
  </button>
  <button
    v-else
    type="button"
    role="menuitem"
    class="session-menu-sleep"
    :disabled="win === null || win.busy || session.sleep_requested || win.nothingToSleep"
    @click="pick((w) => w.sleep())"
  >
    {{ session.sleep_requested ? 'усыпляю…' : 'Усыпить' }}
  </button>
  <!-- Ф71-1 (Р3): Cursor сжимает разговор сам — «Сжать» и «Сжатие…» у него нет (Ф74-1: по профилю). -->
  <button
    v-if="!compactsSelf"
    type="button"
    role="menuitem"
    class="session-menu-compact"
    :disabled="win === null || win.busy || session.state !== 'idle'"
    @click="pick((w) => w.compact())"
  >
    Сжать
  </button>
  <div
    v-if="compactsSelf"
    class="session-menu-compact-note"
    role="note"
  >
    {{ compactNote }}
  </div>
  <button
    v-if="!compactsSelf"
    type="button"
    role="menuitem"
    class="session-menu-compact-settings"
    :disabled="win === null || session.closed_ts !== null"
    @click="pick((w) => w.openCompact())"
  >
    Сжатие…
  </button>
  <!-- Ф99-7.2: «Всегда в этой сессии» — список с «×». -->
  <button
    type="button"
    role="menuitem"
    class="session-menu-always"
    :disabled="win === null"
    @click="pick((w) => w.openAlways())"
  >
    Разрешения…<template v-if="(session.always?.length ?? 0) > 0">
      ({{ session.always?.length }})
    </template>
  </button>
  <!-- Ф73-1: перезапуск процесса с продолжением разговора — движок заново читает MCP-серверы. -->
  <button
    type="button"
    role="menuitem"
    class="session-menu-mcp-reload"
    title="Процесс сессии перезапустится с продолжением разговора и заново прочитает MCP; идущий ход не обрывается"
    :disabled="win === null || win.busy || session.closed_ts !== null || !!session.mcp_reload_ts"
    @click="pick((w) => w.mcpReload())"
  >
    {{ session.mcp_reload_ts ? 'перечитываю MCP…' : 'Перечитать MCP' }}
  </button>
  <button
    type="button"
    role="menuitem"
    class="session-menu-model"
    @click="pick((w) => w.openPanel('model'))"
  >
    Модель и усилие…
  </button>
  <button
    v-if="session.on_fallback"
    type="button"
    role="menuitem"
    class="session-menu-fallback-return"
    :title="session.on_fallback_text ?? undefined"
    :disabled="win === null || win.busy"
    @click="pick((w) => w.returnMain())"
  >
    Вернуть на основной
  </button>
  <button
    type="button"
    role="menuitem"
    class="session-menu-role"
    @click="pick((w) => w.openPanel('role'))"
  >
    Роль…
  </button>
  <button
    type="button"
    role="menuitem"
    class="session-menu-role-text"
    @click="pick((w) => w.openRoleText())"
  >
    Инструкция роли
  </button>
  <button
    type="button"
    role="menuitem"
    class="session-menu-handoff"
    @click="pick((w) => w.openPanel('handoff'))"
  >
    Новая с передачей дел…
  </button>
  <button
    v-if="!isPhone()"
    type="button"
    role="menuitem"
    class="session-menu-interview"
    :disabled="win === null || session.closed_ts !== null"
    @click="pick((w) => w.openPanel('interview'))"
  >
    Интервью…
  </button>
  <button
    v-if="win !== null && win.canTerminal"
    type="button"
    role="menuitem"
    class="session-menu-terminal"
    @click="pick((w) => w.openTerminal())"
  >
    Открыть в терминале
  </button>
  <button
    type="button"
    role="menuitem"
    class="session-menu-sub"
    @click="pick((w) => w.toggleSubEdit())"
  >
    Подписка…
  </button>
  <button
    type="button"
    role="menuitem"
    class="session-menu-close"
    :disabled="win === null || win.busy"
    @click="pick((w) => w.close())"
  >
    Закрыть
  </button>
</template>
