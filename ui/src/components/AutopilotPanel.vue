<script setup lang="ts">
// Ф41-5 (интерфейс, раздел 6, 11 П10/П14): панель «Автопилот» в меню «⋯». Таблица: строки каталога по группам,
// столбцы «Общее» (флажок «придержать») и проекты с рабочей папкой (три положения: «как общее» / «придержать» /
// «сама»). У строки подпись вида правила («карточка» / «запрет») и серые пометки; мета-выключатели — отдельной
// группой внизу, снятие спрашивает подтверждение. «Поставить все» — всё придержать. С телефона меняется всё, как с
// ноутбука (слова владельца; прежнее «только чтение» снято). Ручек ядра нет (404) — пустая панель с пояснением.
import { computed, onBeforeUnmount, onMounted } from 'vue';

import type { AutopilotSwitch } from '../api/types';
import type { CardRoute } from '../api/types';
import {
  autopilotUi,
  holdAll,
  loadAutopilot,
  setCardRoute,
  setCardRouteAll,
  setGlobalMany,
  setProjectMany,
  setSwitch,
} from '../store/autopilot';
import { store } from '../store/instance';
import AgentPatternsSection from './AgentPatternsSection.vue';
import {
  bulkFreeConfirmText,
  CELL_LABEL,
  cellToHold,
  globalHeld,
  groupSwitches,
  isFreeing,
  manualLabel,
  metaConfirmText,
  notesOf,
  projectCell,
  projectFreeConfirmText,
  projectSwitchView,
  switchRowHidden,
  type ProjectCell,
} from '../util/autopilot';
import { isShowcase } from '../util/showcase';
import { CARD_ROUTE_BULK_GROUPS, CARD_ROUTE_GROUPS, CARD_ROUTE_NOTE } from '../util/owner_channel';

const emit = defineEmits<{ close: [] }>();
// Витрина: образцы решений владельца (Ф76) — личное, раздела нет.
const showcase = isShowcase();

const data = computed(() => autopilotUi.data);
const groups = computed(() => (data.value === null ? [] : groupSwitches(data.value.switches)));
const projects = computed(() => store.state.projects.filter((p) => p.id > 0 && p.workdir));
// Ф74-6: строки, мёртвые на всех движках, не показываются; в ячейке проекта — по движкам его живых сессий.
const sessionList = computed(() => Object.values(store.state.sessions.items));
function viewOf(projectId: number, key: string) {
  return projectSwitchView(sessionList.value, projectId, key);
}
const CELLS: ProjectCell[] = ['inherit', 'hold', 'free'];
// Ф66-1: таблица «Кто отвечает на карточки» — по проектам (меняется и с телефона).
const allProjects = computed(() => store.state.projects.filter((p) => p.id > 0));
const ROUTE_CHOICES: { value: CardRoute; label: string }[] = [
  { value: 'me', label: 'Мне' },
  { value: 'agent', label: 'Агенту' },
  { value: 'auto', label: 'Сразу да' },
];

function routeOf(projectId: number, group: string): CardRoute {
  const value = store.state.projects.find((p) => p.id === projectId)?.card_route?.[group];
  return value === 'agent' || value === 'auto' ? value : 'me';
}

function allRouteIs(projectId: number, value: 'me' | 'agent'): boolean {
  return CARD_ROUTE_BULK_GROUPS.every((g) => routeOf(projectId, g.key) === value); // Ф93-1: без «Деньги»
}

function onRoute(projectId: number, group: string, value: CardRoute): void {
  if (routeOf(projectId, group) === value) return;
  void setCardRoute(projectId, { [group]: value });
}

function onRouteAll(projectId: number, value: 'me' | 'agent'): void {
  void setCardRouteAll(projectId, value);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  event.stopPropagation();
  emit('close');
}

onMounted(() => {
  void loadAutopilot();
  document.addEventListener('keydown', onKeydown);
});

onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown));

function confirmFree(s: AutopilotSwitch, hold: boolean | null): boolean {
  if (s.meta !== true || !isFreeing(hold)) return true;
  return window.confirm(metaConfirmText(s));
}

async function onGlobal(s: AutopilotSwitch, event: Event): Promise<void> {
  const box = event.target as HTMLInputElement;
  const hold = box.checked;
  if (!confirmFree(s, hold)) {
    box.checked = !hold; // отказ — флажок возвращается
    return;
  }
  if (!(await setSwitch('global', s.key, hold))) box.checked = !hold;
}

async function onProject(s: AutopilotSwitch, projectId: number, event: Event): Promise<void> {
  const select = event.target as HTMLSelectElement;
  const previous = projectCell(data.value!, projectId, s.key);
  const hold = cellToHold(select.value as ProjectCell);
  if (!confirmFree(s, hold)) {
    select.value = previous;
    return;
  }
  if (!(await setSwitch(projectId, s.key, hold))) select.value = previous;
}

async function onHoldAll(): Promise<void> {
  await holdAll();
}

// Массово (общее положение): «снять» — одно подтверждение со списком того, что реально снимается; «вернуть» — без
// вопроса (возврат ограничений безопасен).
async function onBulkFree(switches: AutopilotSwitch[]): Promise<void> {
  if (data.value === null) return;
  const d = data.value;
  const targets = switches.filter((s) => globalHeld(d, s.key));
  if (targets.length === 0 || !window.confirm(bulkFreeConfirmText(targets))) return;
  await setGlobalMany(
    targets.map((s) => s.key),
    false,
  );
}

// Слова владельца («на одном проекте хочу включить»): весь столбец проекта — «сама» (одно подтверждение со
// списком, мета — в нём же) или «спрашивать» (без вопроса). Скрытые для проекта строки не трогаются.
function projectTargets(projectId: number, cell: ProjectCell): AutopilotSwitch[] {
  if (data.value === null) return [];
  const d = data.value;
  return groups.value
    .flatMap((g) => g.switches)
    .filter((s) => !switchRowHidden(s.key) && !viewOf(projectId, s.key).hidden)
    .filter((s) => projectCell(d, projectId, s.key) !== cell);
}

async function onProjectFree(projectId: number, projectName: string | null): Promise<void> {
  const targets = projectTargets(projectId, 'free');
  if (targets.length === 0 || !window.confirm(projectFreeConfirmText(projectName ?? `#${projectId}`, targets))) return;
  await setProjectMany(
    projectId,
    targets.map((s) => s.key),
    false,
  );
}

async function onProjectHold(projectId: number): Promise<void> {
  const targets = projectTargets(projectId, 'hold');
  if (targets.length === 0) return;
  await setProjectMany(
    projectId,
    targets.map((s) => s.key),
    true,
  );
}

async function onBulkHold(switches: AutopilotSwitch[]): Promise<void> {
  if (data.value === null) return;
  const d = data.value;
  const targets = switches.filter((s) => !globalHeld(d, s.key));
  if (targets.length === 0) return;
  await setGlobalMany(
    targets.map((s) => s.key),
    true,
  );
}
</script>

<template>
  <div
    class="autopilot-overlay"
    @click.self="emit('close')"
  >
    <div
      class="autopilot-panel"
      role="dialog"
      aria-modal="true"
      aria-label="Автопилот"
    >
    <div class="autopilot-head">
      <strong>Автопилот</strong>
      <button
        v-if="data !== null"
        type="button"
        class="autopilot-hold-all"
        @click="onHoldAll"
      >
        Спрашивать обо всём
      </button>
      <button
        v-if="data !== null"
        type="button"
        class="autopilot-free-all"
        @click="onBulkFree(data.switches)"
      >
        Всё делает сама — во всех проектах
      </button>
      <button
        type="button"
        class="access-panel-close autopilot-close"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </div>
    <div class="autopilot-body">
    <AgentPatternsSection v-if="!showcase" />
    <div
      v-if="allProjects.length > 0"
      class="route-table"
      data-test="route-table"
    >
      <strong>Кто отвечает на карточки</strong>
      <div
        v-for="p in allProjects"
        :key="p.id"
        class="route-project"
        :data-route-project="p.id"
      >
        <strong class="route-project-title">{{ p.name }}</strong>
        <div class="route-row route-row--all">
          <span class="route-name">всем:</span>
          <span class="route-buttons">
            <button
              v-for="c in ROUTE_CHOICES.slice(0, 2)"
              :key="c.value"
              type="button"
              class="route-btn"
              :class="{ 'route-btn--on': allRouteIs(p.id, c.value as 'me' | 'agent') }"
              :aria-pressed="allRouteIs(p.id, c.value as 'me' | 'agent')"
              :data-route-all="c.value"
              @click="onRouteAll(p.id, c.value as 'me' | 'agent')"
            >
              {{ c.label }}
            </button>
          </span>
        </div>
        <div
          v-for="g in CARD_ROUTE_GROUPS"
          :key="g.key"
          class="route-row"
          :data-route-group="g.key"
        >
          <span
            class="route-name"
            :title="g.hint"
          >{{ g.label }}<small
            v-if="g.hint"
            class="route-hint"
          >{{ g.hint }}</small></span>
          <span class="route-buttons">
            <template
              v-for="c in ROUTE_CHOICES"
              :key="c.value"
            >
              <button
                v-if="c.value !== 'auto' || g.auto"
                type="button"
                class="route-btn"
                :class="{ 'route-btn--on': routeOf(p.id, g.key) === c.value }"
                :aria-pressed="routeOf(p.id, g.key) === c.value"
                :data-route="c.value"
                @click="onRoute(p.id, g.key, c.value)"
              >
                {{ c.label }}
              </button>
              <span
                v-else
                class="route-btn route-btn--none"
                aria-hidden="true"
              />
            </template>
          </span>
        </div>
      </div>
      <p class="autopilot-note">
        {{ CARD_ROUTE_NOTE }}
      </p>
      <p
        v-if="autopilotUi.error"
        class="autopilot-error"
        role="alert"
      >
        {{ autopilotUi.error }}
      </p>
    </div>
    <p class="autopilot-intro">
      Галочка стоит — на этом сессия в авто-режиме останавливается и спрашивает тебя (карточка или запрет).
      Галочка снята — делает сама.
    </p>
    <p
      v-if="data === null"
      class="autopilot-empty"
    >
      {{ autopilotUi.loaded ? 'Ядро пока не отдаёт выключатели автопилота.' : 'Загрузка…' }}
    </p>
    <template v-else>
      <p
        v-if="autopilotUi.error"
        class="autopilot-error"
        role="alert"
      >
        {{ autopilotUi.error }}
      </p>
      <table class="autopilot-table">
        <thead>
          <tr>
            <th class="autopilot-first">
              Действие
            </th>
            <th class="autopilot-col">
              Спрашивать меня
            </th>
            <th
              v-for="p in projects"
              :key="p.id"
              class="autopilot-col"
              :data-project-col="p.id"
            >
              {{ p.name }}
              <button
                type="button"
                class="link-button autopilot-project-free"
                @click="onProjectFree(p.id, p.name)"
              >
                всё сама
              </button>
              <button
                type="button"
                class="link-button autopilot-project-hold"
                @click="onProjectHold(p.id)"
              >
                всё спрашивать
              </button>
            </th>
          </tr>
        </thead>
        <template
          v-for="group in groups"
          :key="group.title"
        >
          <tbody
            class="autopilot-group"
            :class="{ 'autopilot-group--meta': group.meta }"
            :data-group="group.title"
          >
            <tr class="autopilot-group-row">
              <th
                :colspan="2 + projects.length"
                class="autopilot-group-title"
              >
                {{ group.title }}
                <button
                  type="button"
                  class="link-button autopilot-group-free"
                  @click="onBulkFree(group.switches)"
                >
                  снять группу
                </button>
                <button
                  type="button"
                  class="link-button autopilot-group-return"
                  @click="onBulkHold(group.switches)"
                >
                  вернуть группу
                </button>
              </th>
            </tr>
            <tr
              v-for="s in group.switches.filter((x) => !switchRowHidden(x.key))"
              :key="s.key"
              class="autopilot-row"
              :data-switch="s.key"
            >
              <td class="autopilot-name autopilot-first">
                <span class="autopilot-title">{{ s.title }}</span>
                <span class="autopilot-kind">{{ manualLabel(s) }}</span>
                <span
                  v-for="note in notesOf(s)"
                  :key="note"
                  class="autopilot-note"
                >{{ note }}</span>
              </td>
              <td>
                <label class="autopilot-global">
                  <input
                    type="checkbox"
                    :checked="globalHeld(data, s.key)"
                    :aria-label="`Спрашивать меня: ${s.title}`"
                    @change="onGlobal(s, $event)"
                  >
                </label>
              </td>
              <td
                v-for="p in projects"
                :key="p.id"
              >
                <span
                  v-if="viewOf(p.id, s.key).hidden"
                  class="autopilot-note autopilot-dead"
                  :data-project-id="p.id"
                >{{ viewOf(p.id, s.key).note }}</span>
                <select
                  v-else
                  class="autopilot-cell"
                  :data-project-id="p.id"
                  :value="projectCell(data, p.id, s.key)"
                  :aria-label="`${s.title} — ${p.name}`"
                  @change="onProject(s, p.id, $event)"
                >
                  <option
                    v-for="cell in CELLS"
                    :key="cell"
                    :value="cell"
                  >
                    {{ CELL_LABEL[cell] }}
                  </option>
                </select>
                <span
                  v-if="!viewOf(p.id, s.key).hidden && viewOf(p.id, s.key).note"
                  class="autopilot-note autopilot-dead"
                >{{ viewOf(p.id, s.key).note }}</span>
              </td>
            </tr>
          </tbody>
        </template>
      </table>
    </template>
    </div>
    </div>
  </div>
</template>
