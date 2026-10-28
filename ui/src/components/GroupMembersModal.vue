<script setup lang="ts">
// Ф62-6 (раздел G): «Участники…» в шапке беседы-группы WhatsApp — окно поверх. При открытии просит свежий состав
// (`GET /channels/{id}/group-members?refresh=1`), пока идёт задание — показывает прежний снимок (нет его —
// «загружаю…»), потом перечитывает. У каждого: имя, номер, «админ», у себя «я»; «Убрать» (с подтверждением) —
// у всех, кроме себя. «Добавить» (поиск «Кому», номер вручную) и «Убрать» -> `POST /group-ops/ui` -> строка хода
// -> итог по участникам («приватность — пошли ссылку», «нет WhatsApp» …) и обновление состава. Имена — чужой
// текст, только интерполяция. Закрывается по ✕, Esc, клику мимо.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { ApiError, getGroupMembers } from '../api/client';
import type { GroupMember, GroupMembers, GroupOp } from '../api/types';
import { store } from '../store/instance';
import {
  groupOpClosed,
  groupOpErrorText,
  groupOpOutcomeLines,
  groupOpProgress,
  participantsLine,
  type PickedPerson,
} from '../util/groupOps';
import GroupPeoplePicker from './GroupPeoplePicker.vue';

const props = defineProps<{ channelId: number; title: string }>();
const emit = defineEmits<{ close: [] }>();

const POLL_MS = 3000;

const dialog = ref<HTMLElement | null>(null);
const action = ref<'add' | 'remove'>('add');
const people = ref<PickedPerson[]>([]);
const busy = ref(false);
const error = ref<string | null>(null);
const opId = ref<number | null>(null);

// Аккаунт беседы — из её строки (`account_id` приходит в списке бесед и `/sync`).
const accountId = computed<number | null>(() => store.state.sync.channelsById[props.channelId]?.account_id ?? null);
const roster = ref<GroupMembers | null>(null);
const rosterError = ref<string | null>(null);
const rosterLoading = ref(false);
const history = computed<GroupOp[]>(() =>
  Object.values(store.state.groupOps.items)
    .filter((o) => o.channel_id === props.channelId && (o.kind === 'add' || o.kind === 'remove'))
    .sort((a, b) => b.id - a.id)
    .slice(0, 5),
);
const op = computed<GroupOp | null>(() => (opId.value === null ? null : (store.state.groupOps.items[opId.value] ?? null)));
const running = computed(() => op.value !== null && !groupOpClosed(op.value));
const canSubmit = computed(() => !busy.value && !running.value && people.value.length > 0);

watch(action, () => {
  people.value = [];
  error.value = null;
});

let poll: ReturnType<typeof setInterval> | null = null;
let rosterPoll: ReturnType<typeof setInterval> | null = null;
let rosterSeq = 0;
let alive = true;

function stopPoll(): void {
  if (poll !== null) clearInterval(poll);
  poll = null;
}

function stopRosterPoll(): void {
  if (rosterPoll !== null) clearInterval(rosterPoll);
  rosterPoll = null;
}

// Состав: `refresh` — просит свежий у моста; ждёт исход задания `members` и перечитывает без `refresh`.
async function loadRoster(refresh: boolean): Promise<void> {
  rosterSeq += 1;
  const mine = rosterSeq;
  stopRosterPoll();
  rosterLoading.value = true;
  try {
    const data = await getGroupMembers(props.channelId, refresh);
    if (!alive || mine !== rosterSeq) return;
    roster.value = data;
    rosterError.value = null;
    const job = data.refresh;
    if (job === null) {
      rosterLoading.value = false;
      return;
    }
    rosterPoll = setInterval(() => {
      void store
        .refreshGroupOp(job.op_id)
        .then(async (done) => {
          if (!alive || mine !== rosterSeq || !groupOpClosed(done)) return;
          stopRosterPoll();
          if (done.status === 'failed') {
            rosterLoading.value = false;
            rosterError.value = `Состав не обновился: ${groupOpErrorText(done.error)}.`;
            return;
          }
          const fresh = await getGroupMembers(props.channelId, false);
          if (!alive || mine !== rosterSeq) return;
          roster.value = fresh;
          rosterLoading.value = false;
        })
        .catch(() => undefined);
    }, POLL_MS);
  } catch (err) {
    if (!alive || mine !== rosterSeq) return;
    rosterLoading.value = false;
    rosterError.value =
      err instanceof ApiError && err.status === 422 ? 'Это не группа WhatsApp.' : 'Состав не загрузился — попробуйте ещё раз.';
  }
}

const noSnapshot = computed(() => roster.value === null || roster.value.updated_ts === null);

function memberLabel(m: GroupMember): string {
  return m.name?.trim() || m.phone?.trim() || m.phone_masked?.trim() || 'без имени';
}

function memberPhone(m: GroupMember): string {
  const number = m.phone?.trim() || m.phone_masked?.trim() || '';
  return number === memberLabel(m) ? '' : number;
}

async function removeMember(m: GroupMember): Promise<void> {
  const value = m.phone?.trim() || m.jid;
  if (!value || busy.value || running.value) return;
  if (!window.confirm(`Убрать из «${props.title}»: ${memberLabel(m)}?`)) return;
  await send('remove', [value]);
}

watch(op, (current) => {
  if (current !== null && groupOpClosed(current)) {
    stopPoll();
    if (current.status === 'done') void loadRoster(true);
    if (current.status === 'failed') error.value = `Не вышло: ${groupOpErrorText(current.error)}.`;
    else if (current.status === 'declined') error.value = 'Отменено.';
  }
});

async function submit(): Promise<void> {
  if (!canSubmit.value) return;
  if (action.value === 'remove') {
    const who = people.value.map((p) => p.label).join(', ');
    if (!window.confirm(`Убрать из «${props.title}»: ${who}?`)) return;
  }
  await send(action.value, people.value.map((p) => p.value));
}

async function send(kind: 'add' | 'remove', participants: string[]): Promise<void> {
  busy.value = true;
  error.value = null;
  try {
    const created = await store.submitGroupOp({ kind, channelId: props.channelId, participants });
    opId.value = created.id;
    people.value = [];
    stopPoll();
    poll = setInterval(() => void store.refreshGroupOp(created.id).catch(() => undefined), POLL_MS);
  } catch (err) {
    error.value =
      err instanceof ApiError && [403, 404, 409, 422, 429].includes(err.status) && err.message
        ? err.message
        : 'Не отправилось — проверьте соединение и попробуйте ещё раз.';
  } finally {
    busy.value = false;
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return;
  event.stopPropagation();
  emit('close');
}

function kindText(o: GroupOp): string {
  return o.kind === 'add' ? 'Добавить' : 'Убрать';
}

function statusText(o: GroupOp): string {
  if (o.status === 'failed') return `не вышло: ${groupOpErrorText(o.error)}`;
  if (o.status === 'declined') return 'отменено';
  if (o.status === 'done') return 'готово';
  return groupOpProgress(o);
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown);
  void nextTick(() => dialog.value?.focus());
  void store.loadGroupOps(props.channelId);
  void loadRoster(true);
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown);
  alive = false;
  stopPoll();
  stopRosterPoll();
});
</script>

<template>
  <Teleport to="body">
    <div
      class="channel-modal-overlay group-members-overlay"
      @click.self="emit('close')"
    >
      <div
        ref="dialog"
        class="channel-modal role-text-modal group-members-modal"
        role="dialog"
        aria-modal="true"
        :aria-label="`Участники: ${title}`"
        tabindex="-1"
      >
        <header class="channel-modal-head">
          <b class="channel-modal-title">Участники · {{ title }}</b>
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
        <div class="role-text-body group-modal-body">
          <p
            v-if="rosterLoading && noSnapshot"
            class="role-text-loading group-roster-loading"
          >
            загружаю…
          </p>
          <ul
            v-if="roster !== null && roster.members.length > 0"
            class="group-roster"
          >
            <li
              v-for="(m, idx) in roster.members"
              :key="m.jid ?? `m${idx}`"
              class="group-roster-item"
            >
              <span class="group-roster-name">{{ memberLabel(m) }}</span>
              <span
                v-if="memberPhone(m) !== ''"
                class="group-roster-phone"
              >{{ memberPhone(m) }}</span>
              <span
                v-if="m.is_admin"
                class="group-roster-tag"
              >админ</span>
              <span
                v-if="m.is_me"
                class="group-roster-tag"
              >я</span>
              <button
                v-if="!m.is_me"
                type="button"
                class="group-roster-remove"
                :disabled="busy || running"
                @click="removeMember(m)"
              >
                Убрать
              </button>
            </li>
          </ul>
          <p
            v-else-if="roster !== null && !rosterLoading && !noSnapshot"
            class="group-op-reason"
          >
            Участников в снимке нет.
          </p>
          <p
            v-if="rosterLoading && !noSnapshot"
            class="group-op-reason"
          >
            обновляю состав…
          </p>
          <p
            v-if="rosterError"
            class="new-message-error"
            role="alert"
          >
            {{ rosterError }}
          </p>
          <div class="group-mode-switch">
            <button
              type="button"
              :aria-pressed="action === 'add'"
              @click="action = 'add'"
            >
              Добавить
            </button>
            <button
              type="button"
              :aria-pressed="action === 'remove'"
              @click="action = 'remove'"
            >
              Убрать
            </button>
          </div>
          <GroupPeoplePicker
            v-model="people"
            :account-id="accountId"
            :placeholder="action === 'add' ? 'кого добавить: имя или номер +7…' : 'кого убрать: имя или номер +7…'"
          />
          <div class="new-message-actions">
            <button
              type="button"
              class="group-members-submit"
              :disabled="!canSubmit"
              @click="submit"
            >
              {{ action === 'add' ? 'Добавить' : 'Убрать' }}
            </button>
          </div>
          <p
            v-if="op !== null && running"
            class="new-message-hint group-members-progress"
          >
            {{ groupOpProgress(op) }}
          </p>
          <ul
            v-if="op !== null && groupOpOutcomeLines(op).length > 0"
            class="group-op-result"
          >
            <li
              v-for="line in groupOpOutcomeLines(op)"
              :key="line"
            >
              {{ line }}
            </li>
          </ul>
          <p
            v-if="error"
            class="new-message-error"
            role="alert"
          >
            {{ error }}
          </p>
          <template v-if="history.length > 0">
            <p class="group-op-reason">
              Последние заявки
            </p>
            <ul class="group-op-result group-members-history">
              <li
                v-for="item in history"
                :key="item.id"
              >
                {{ kindText(item) }}: {{ participantsLine(item) }} — {{ statusText(item) }}
              </li>
            </ul>
          </template>
        </div>
      </div>
    </div>
  </Teleport>
</template>
