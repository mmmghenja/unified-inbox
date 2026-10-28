<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';

import { listSubs } from '../api/client';

import type { AttentionMode, ChanSub, Message } from '../api/types';
import { store } from '../store/instance';
import { ATTENTION_CHOICES, ATTENTION_LABELS, AUTO, autoLabel } from '../util/attention';
import { confirmAndHideChannel } from '../util/hide';
import { newRecipientWho } from '../util/home';
import { sourceLabel } from '../util/people';
import {
  canLeaveChannel,
  LEFT_NOTICE,
  leaveButtonLabel,
  leaveButtonState,
  leaveConfirmText,
} from '../util/leaves';
import { layout } from '../util/layout';
import { claudeSessionsLine, mapReasonLabel, projectSelectOptions, projectSelectValue, targetOf } from '../util/mapping';
import { replyPreviewLabel } from '../util/replyPreview';
import { isWhatsappGroup } from '../util/groupOps';
import { accountAsleep, CHANNEL_SLEEP_NOTE } from '../util/workday';
import { badgeLabel, pendingOfChannel } from '../util/reminders';
import AutosendControl from './AutosendControl.vue';
import ChannelReminders from './ChannelReminders.vue';
import GroupLinkModal from './GroupLinkModal.vue';
import GroupMembersModal from './GroupMembersModal.vue';
import Composer from './Composer.vue';
import DraftCard from './DraftCard.vue';
import MessageFeed from './MessageFeed.vue';
import NoReplyNotice from './NoReplyNotice.vue';
import ChannelNo from './ChannelNo.vue';
import StageControl from './StageControl.vue';
import ThreadsMenu from './ThreadsMenu.vue';
import { listeningLabelOf } from '../util/listeners';
import { usePopup } from '../util/popup';

// Ф80-1 D1: `actionsOnly` — только действия с беседой (проект, стадия, режим, «Без подтверждения», «Напомнить…», «Заглушить»,
// «Удалить…», «Выйти»…) без ленты и поля ввода: столбец доски и окно беседы показывают их в своём «⋯».
const props = defineProps<{ channelId: number; actionsOnly?: boolean }>();

const channel = computed(() => store.state.sync.channelsById[props.channelId] ?? null);
// Ф70-1 D.1: канал спит вместе с днём проекта — серая пометка в шапке; поле ввода работает как обычно.
const channelAsleep = computed(() => accountAsleep(store.state.status?.accounts, channel.value?.account_id));
// Ф10-5 B5: беседа трекера — ответа из хаба нет; поля нет у старого ядра -> поле ввода как раньше.
const canReply = computed(() => channel.value?.can_reply !== false);

// Ф12-5 C3: беседа-заготовка нового адресата — над полем ввода строка «Новая беседа · кому: …». Ф14-2 D3: для
// всех видов адресата; неизвестный вид — без строки.
const newRecipientLine = computed(() => {
  const newTo = channel.value?.new_to;
  // Ф46-1: личка автору сообщения группы — «Новая личка · из группы «…»» (имя автора — в названии беседы).
  if (newTo?.kind === 'tg_msg_author') {
    return newTo.from_title ? `Новая личка · из группы «${newTo.from_title}»` : `Новая личка · кому: ${newTo.name}`;
  }
  const who = newRecipientWho(newTo);
  return who === null ? null : `Новая беседа · кому: ${who}`;
});
// Ф46-1: «к сообщению» — переход в группу на сообщение, откуда взят автор (нет полей — без ссылки).
const authorSource = computed(() => {
  const newTo = channel.value?.new_to;
  if (newTo?.kind !== 'tg_msg_author') return null;
  if (typeof newTo.from_channel_id !== 'number' || typeof newTo.from_message_id !== 'number') return null;
  return { channelId: newTo.from_channel_id, messageId: newTo.from_message_id };
});

function openAuthorSource(): void {
  const src = authorSource.value;
  if (src === null) return;
  const projectId = store.state.sync.channelsById[src.channelId]?.project_id ?? null;
  void store.openChannelAt(projectId, src.channelId, { messageId: src.messageId });
}

// Ф10-6 B3: в `half`/`phone` шапка — название, «Треды» и меню «⋯» с остальным (режим, заглушить, «Сделать
// задачей», «Удалить беседу из хаба»); в `full` — всё в строку, как раньше. Телефон — «‹» назад к списку.
const compactHeader = computed(() => props.actionsOnly !== true);
const isPhone = computed(() => layout.mode === 'phone');
const menuOpen = ref(false);
// Ф80-1 A2: меню «⋯» — Esc, нажатие мимо и открытие другого меню закрывают его (общий механизм `util/popup`).
const feedMenuButton = ref<HTMLElement | null>(null);
const feedActions = ref<HTMLElement | null>(null);
usePopup({
  open: menuOpen,
  button: () => feedMenuButton.value,
  panel: () => (compactHeader.value ? feedActions.value : null),
});

function closeMenu(): void {
  menuOpen.value = false;
}

function back(): void {
  store.goBack('channel');
}

function toggleMute(): void {
  closeMenu();
  if (channel.value === null) return;
  void store.muteChannel(props.channelId, !channel.value.muted);
}

// Ф8-4 B4: «Удалить беседу из хаба» — с подтверждением; беседа пропадает из списка, открытая закрывается.
function hideChannel(): void {
  closeMenu();
  void confirmAndHideChannel(props.channelId);
}

// Ф17-2b: «Выйти из группы / канала / форума» — беседа Telegram `group|channel|topic` не в архиве. Состояние —
// по заявкам беседы: сами грузим `GET /leaves?channel_id=` при открытии (П12: иначе при прямом открытии беседы
// «Выхожу…» и «не вышло» не видны). У темы заявки принадлежат форуму — их `channel_id` запоминаем из ответа.
const showLeave = computed(() => canLeaveChannel(channel.value));
// Ф62-6: меню группы WhatsApp — «Участники…» и «Ссылка-приглашение» окнами поверх.
const showGroupMenu = computed(() => isWhatsappGroup(channel.value));
const membersOpen = ref(false);
const linkOpen = ref(false);

function openMembers(): void {
  closeMenu();
  membersOpen.value = true;
}

function openLink(): void {
  closeMenu();
  linkOpen.value = true;
}
const leaveTargets = ref<number[]>([]);
const channelLeaves = computed(() =>
  Object.values(store.state.leaves.items).filter(
    (l) => l.channel_id === props.channelId || leaveTargets.value.includes(l.channel_id),
  ),
);
const leaveState = computed(() => leaveButtonState(channelLeaves.value, Date.now()));
const leaveError = ref<string | null>(null);
const leaveBusy = ref(false);
const leftNotice = computed(() => channel.value?.archived === true && leaveState.value.left);

const leaveLoadable = computed(() => {
  const c = channel.value;
  return c !== null && c.source === 'telegram' && (c.kind === 'group' || c.kind === 'channel' || c.kind === 'topic');
});

async function loadLeaves(): Promise<void> {
  const id = props.channelId;
  const targets = await store.loadLeaves(id);
  if (id === props.channelId) leaveTargets.value = targets;
}

async function leave(): Promise<void> {
  closeMenu();
  if (channel.value === null || leaveBusy.value || leaveState.value.leaving) return;
  if (!window.confirm(leaveConfirmText(channel.value.title, channel.value.source))) return;
  leaveBusy.value = true;
  leaveError.value = null;
  try {
    await store.leaveChannel(props.channelId);
    await loadLeaves();
  } catch (err) {
    leaveError.value = err instanceof Error && err.message ? err.message : 'Не удалось выйти — попробуйте ещё раз.';
  } finally {
    leaveBusy.value = false;
  }
}

// Ф8-3 D1: режим беседы — «Авто (сейчас: …)» (ручного нет, `attention_manual: null`) или ручной; выбор —
// `PATCH attention` («Авто» -> `null`). Кнопка «Заглушить» остаётся отдельно.
const attentionValue = computed(() => channel.value?.attention_manual ?? AUTO);

function onAttentionChange(event: Event): void {
  const value = (event.target as HTMLSelectElement).value;
  const mode = value === AUTO ? null : (value as AttentionMode);
  closeMenu();
  if ((channel.value?.attention_manual ?? null) === mode) return;
  void store.setChannelAttention(props.channelId, mode);
}

// Ф14-1 C1: проект беседы — «По правилам» (авто), настоящий проект или «Неразобранное» (вручную); смена —
// `PATCH` (`project_id` / `null` / `map_source: "auto"`), затем `/sync` и проекты. Рядом серым — причина
// раскладки; у проекта с рабочей папкой — строка про сессии Claude. Ошибка — текстом в шапке, выбор — назад.
const projectOptions = computed(() => projectSelectOptions(store.state.projects, channel.value));
const projectValue = computed(() => (channel.value === null ? '' : projectSelectValue(channel.value)));
const reasonLabel = computed(() => mapReasonLabel(channel.value?.map_reason));
const claudeLine = computed(() => claudeSessionsLine(store.state.projects, channel.value?.project_id ?? null));
const projectError = ref<string | null>(null);
// Перерисовать `<select>` после отказа: значение из стора не менялось, а DOM уже показывает выбранное.
const projectSelectKey = ref(0);

async function onProjectChange(event: Event): Promise<void> {
  const value = (event.target as HTMLSelectElement).value;
  closeMenu();
  const target = targetOf(value);
  if (target === undefined || value === projectValue.value) return;
  projectError.value = null;
  try {
    await store.setChannelProject(props.channelId, target);
  } catch (err) {
    projectError.value = err instanceof Error && err.message ? `Не удалось перенести: ${err.message}` : 'Не удалось перенести.';
    projectSelectKey.value += 1;
  }
}

// Спека 2e, раздел «Семантика «Ответить» по источнику»: режим ответа Telegram-стиля (плашка над полем
// ввода) — состояние живёт здесь, у общего предка ленты (источник клика) и композера (получатель `replyTo`).
// Slack-ветка сюда не попадает вовсе — MessageRow.vue для Slack-канала открывает тред напрямую через стор.
const replyTarget = ref<{ extId: string; preview: string } | null>(null);

function onReply(message: Message): void {
  if (message.ext_id === null) return;
  replyTarget.value = { extId: message.ext_id, preview: replyPreviewLabel(message) };
}

function cancelReply(): void {
  replyTarget.value = null;
}

// Ф8-6 C2: черновики беседы (все, по порядку) закреплены над полем ввода; строка черновика в ленте остаётся.
const pinnedDrafts = computed(() => store.draftsOf(props.channelId));

// Ф14-5 D/П9: пометка «слушает сессия …» — беседа сама спрашивает `GET /subs?live=1` при открытии и раз в 60 с;
// сбой запроса молча оставляет прошлый ответ.
const CHAN_SUBS_POLL_MS = 60_000;
const chanSubs = ref<ChanSub[]>([]);
let chanSubsTimer: ReturnType<typeof setInterval> | null = null;

async function loadChanSubs(): Promise<void> {
  try {
    const body = await listSubs();
    chanSubs.value = Array.isArray(body?.channels) ? body.channels : [];
  } catch {
    // пометка — подсказка; ошибку не показываем
  }
}

// Ф39 C: слушающих может быть несколько — «слушают: роль, роль».
const listeningLabel = computed(() => listeningLabelOf(chanSubs.value, props.channelId));

// Ф34-2: напоминания беседы — значок «⏰ <когда>» (несколько — число), панель со списком и «Напомнить…».
const remindersOpen = ref(false);
const pendingReminders = computed(() =>
  pendingOfChannel(Object.values(store.state.reminders.items), props.channelId),
);

function toggleReminders(): void {
  closeMenu();
  remindersOpen.value = !remindersOpen.value;
}

onMounted(() => {
  void store.loadReminders(props.channelId);
  if (props.actionsOnly) return;
  void store.loadDrafts();
  void loadChanSubs();
  chanSubsTimer = setInterval(() => void loadChanSubs(), CHAN_SUBS_POLL_MS);
});

onUnmounted(() => {
  if (chanSubsTimer !== null) clearInterval(chanSubsTimer);
  chanSubsTimer = null;
});

watch(
  () => props.channelId,
  () => {
    replyTarget.value = null;
    remindersOpen.value = false;
    void store.loadReminders(props.channelId);
    menuOpen.value = false;
    projectError.value = null;
    leaveTargets.value = [];
    leaveError.value = null;
    void store.loadDrafts();
    void loadChanSubs();
  },
);

watch(
  [() => props.channelId, leaveLoadable],
  ([, loadable]) => {
    if (loadable) void loadLeaves();
  },
  { immediate: true },
);
</script>

<template>
  <section
    class="column column-feed"
    :class="{ 'column-feed--actions-only': actionsOnly }"
  >
    <header
      class="feed-header"
      :class="{ 'feed-header--compact': compactHeader, 'feed-header--actions-only': actionsOnly }"
    >
      <template v-if="!actionsOnly">
      <button
        v-if="isPhone"
        type="button"
        class="back-button"
        aria-label="Назад к списку бесед"
        @click="back"
      >
        ‹
      </button>
      <h2>{{ channel?.title ?? '(канал)' }}</h2>
      <ChannelNo
        v-if="channel"
        :id="channel.id"
      />
      <span
        v-if="channel?.source"
        class="source-label"
        :title="`Источник: ${sourceLabel(channel.source)}`"
      >{{ sourceLabel(channel.source) }}</span>
      <span
        v-if="channelAsleep"
        class="channel-sleep-mark"
        data-test="channel-sleep"
        title="Приём остановлен до «Начать день»; ручная отправка уходит"
      >{{ CHANNEL_SLEEP_NOTE }}</span>
      <span
        v-if="listeningLabel"
        class="chan-sub-mark"
        :title="`${listeningLabel} — сессии подписаны на эту беседу и получают сигнал о новых сообщениях`"
      >{{ listeningLabel }}</span>
      <!-- Ф37-1 A4: чип — когда беседа разрешена без подтверждения хотя бы одной роли -->
      <span
        v-if="(channel?.autosend ?? []).length > 0"
        class="autosend-chip"
        title="Сессии с этими ролями отправляют в беседу сразу, без вашего подтверждения"
      >без подтверждения: {{ (channel?.autosend ?? []).join(', ') }}</span>
      <button
        v-if="pendingReminders.length > 0"
        type="button"
        class="reminder-badge"
        :aria-expanded="remindersOpen"
        title="Напоминания беседы"
        @click="toggleReminders"
      >
        {{ badgeLabel(pendingReminders) }}
      </button>
      <ThreadsMenu
        v-if="(channel?.unread_threads ?? 0) > 0"
        :channel-id="channelId"
      />
      <button
        v-if="compactHeader"
        ref="feedMenuButton"
        type="button"
        class="feed-menu-button"
        aria-label="Действия с беседой"
        :aria-expanded="menuOpen"
        @click="menuOpen = !menuOpen"
      >
        ⋯
      </button>
      </template>
      <div
        v-show="!compactHeader || menuOpen"
        ref="feedActions"
        class="feed-actions"
        :class="{ 'feed-actions--menu': compactHeader, 'feed-actions--stack': actionsOnly }"
      >
        <span class="map-project">
          <select
            :key="projectSelectKey"
            class="project-select"
            aria-label="Проект беседы"
            :value="projectValue"
            @change="onProjectChange"
          >
            <option
              v-for="option in projectOptions"
              :key="option.value"
              :value="option.value"
            >
              {{ option.label }}
            </option>
          </select>
          <span
            v-if="reasonLabel !== null"
            class="map-reason"
          >{{ reasonLabel }}</span>
        </span>
        <StageControl :channel-id="channelId" />
        <span
          v-if="(compactHeader || actionsOnly) && claudeLine !== null"
          class="map-claude-line"
        >{{ claudeLine }}</span>
        <select
          class="attention-select"
          aria-label="Режим беседы"
          :value="attentionValue"
          @change="onAttentionChange"
        >
          <option :value="AUTO">
            {{ autoLabel(channel?.attention_auto) }}
          </option>
          <option
            v-for="mode in ATTENTION_CHOICES"
            :key="mode"
            :value="mode"
          >
            {{ ATTENTION_LABELS[mode] }}
          </option>
        </select>
        <span
          v-if="compactHeader && attentionValue === AUTO"
          class="attention-note"
        >{{ autoLabel(channel?.attention_auto) }}</span>
        <AutosendControl :channel-id="channelId" />
        <button
          v-if="channel?.archived !== true"
          type="button"
          class="remind-button"
          :aria-expanded="remindersOpen"
          @click="toggleReminders"
        >
          Напомнить…
        </button>
        <button
          type="button"
          class="mute-button"
          :title="channel?.muted && channel?.muted_by_role ? `Заглушила сессия «${channel.muted_by_role}»: ${channel.muted_by_note ?? ''}` : undefined"
          @click="toggleMute"
        >
          {{ channel?.muted ? 'Включить звук' : 'Заглушить' }}<span
            v-if="channel?.muted && channel?.muted_by_role"
            class="muted-by"
          > · заглушила «{{ channel.muted_by_role }}»</span>
        </button>
        <button
          v-if="showGroupMenu"
          type="button"
          class="group-members-button"
          @click="openMembers"
        >
          Участники…
        </button>
        <button
          v-if="showGroupMenu"
          type="button"
          class="group-link-button"
          @click="openLink"
        >
          Ссылка-приглашение
        </button>
        <button
          v-if="showLeave"
          type="button"
          class="leave-channel-button"
          :disabled="leaveBusy || leaveState.leaving"
          @click="leave"
        >
          {{ leaveState.leaving ? 'Выхожу…' : leaveButtonLabel(channel!.kind) }}
        </button>
        <span
          v-if="showLeave && leaveState.failure !== null"
          class="leave-failure"
        >не вышло: {{ leaveState.failure }}</span>
        <button
          type="button"
          class="hide-channel-button"
          @click="hideChannel"
        >
          Удалить беседу из хаба
        </button>
      </div>
      <span
        v-if="projectError !== null"
        class="map-error"
        role="alert"
      >{{ projectError }}</span>
      <span
        v-if="leaveError !== null"
        class="map-error"
        role="alert"
      >{{ leaveError }}</span>
    </header>
    <GroupMembersModal
      v-if="membersOpen"
      :channel-id="channelId"
      :title="channel?.title ?? ''"
      @close="membersOpen = false"
    />
    <GroupLinkModal
      v-if="linkOpen"
      :channel-id="channelId"
      :title="channel?.title ?? ''"
      @close="linkOpen = false"
    />
    <p
      v-if="!compactHeader && !actionsOnly && claudeLine !== null"
      class="map-claude-line map-claude-line--under"
    >
      {{ claudeLine }}
    </p>
    <ChannelReminders
      v-if="remindersOpen"
      :channel-id="channelId"
      :archived="channel?.archived === true"
      @close="remindersOpen = false"
    />
    <template v-if="!actionsOnly">
    <MessageFeed
      :channel-id="channelId"
      @reply="onReply"
    />
    <div
      v-if="pinnedDrafts.length > 0"
      class="pinned-drafts"
      aria-label="Черновики беседы"
    >
      <DraftCard
        v-for="item in pinnedDrafts"
        :key="item.id"
        :item="item"
        :show-address="false"
        compact
      />
    </div>
    <p
      v-if="newRecipientLine"
      class="new-recipient-line"
    >
      {{ newRecipientLine }}
      <button
        v-if="authorSource"
        type="button"
        class="author-source-link"
        @click="openAuthorSource"
      >
        к сообщению
      </button>
    </p>
    <p
      v-if="leftNotice"
      class="leave-note"
      role="status"
    >
      {{ LEFT_NOTICE }}
    </p>
    <Composer
      v-else-if="canReply"
      :channel-id="channelId"
      :reply-to="replyTarget"
      @cancel-reply="cancelReply"
    />
    <NoReplyNotice
      v-else
      :channel="channel"
    />
    </template>
  </section>
</template>
