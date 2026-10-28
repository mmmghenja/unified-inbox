<script setup lang="ts">
// Ф12-5 C2 (docs/architecture.md), переделка Ф14-2 D2 (docs/architecture.md):
// «Новое сообщение» — сначала «Кому»: поле с поиском (`GET /recipients`, от 2 символов, пауза 250 мс, устаревший
// ответ не перетирает новый) и список «Беседы» (выбор открывает существующую беседу, заготовка не заводится) /
// «Люди» (новая беседа с человеком с его аккаунта; люди Slack одного аккаунта — чипами, до 8) / «Новый адресат»
// (из самого ввода: `@name`/`t.me/…` -> Telegram, адрес -> письмо). «Откуда» — по адресату (`util/newMessage.ts`),
// «Тема» — только почта, «Проект» — «По правилам» или проект (по умолчанию — проект открытой вкладки). «Создать»
// (Enter) -> `POST /channels/new` -> беседа открыта, фокус в поле ввода. Ошибка ядра — текстом, форма открыта.
// Esc — «Отмена». Всё от сервера (имена, адреса, названия) — только текстом.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { ApiError, searchRecipients } from '../api/client';
import type { Account, RecipientChannel, RecipientHit, RecipientPerson } from '../api/types';
import { store } from '../store/instance';
import { layout } from '../util/layout';
import NewGroupForm from './NewGroupForm.vue';
import {
  RECIPIENT_DEBOUNCE_MS,
  RECIPIENT_MIN_CHARS,
  accountLabel,
  buildChannelNew,
  fromChoice,
  groupMaxPeople,
  isGroupTarget,
  needsSubject,
  parseNewAddress,
  phoneLimitHint,
  personLabel,
  pickPerson,
  removeSlackPerson,
  type NewAddress,
  type Target,
} from '../util/newMessage';

const props = defineProps<{ projectId: number | null }>();
const emit = defineEmits<{ done: [channelId: number]; cancel: [] }>();

const statusAccounts = computed<Account[]>(() => store.state.status?.accounts ?? []);
// Ф62-6: при аккаунте WhatsApp — переключатель «Сообщение / Новая группа».
const waAccounts = computed<Account[]>(() => statusAccounts.value.filter((a) => a.source === 'whatsapp'));
const mode = ref<'message' | 'group'>('message');
const projects = computed(() => store.state.projects.filter((p) => p.id > 0));

const query = ref('');
const hits = ref<RecipientHit[]>([]);
const searchFailed = ref(false);
const target = ref<Target | null>(null);
const chosenAccount = ref<number | null>(null);
const chosenProject = ref<string>(props.projectId !== null && props.projectId > 0 ? String(props.projectId) : '');
const subject = ref('');
const busy = ref(false);
const error = ref<string | null>(null);
const toInput = ref<HTMLInputElement | null>(null);

const phone = computed(() => layout.mode === 'phone');
const channelHits = computed(() => hits.value.filter((h): h is RecipientChannel => h.type === 'channel'));
const personHits = computed(() => hits.value.filter((h): h is RecipientPerson => h.type === 'person'));
// Номер предлагается только если поиск по номеру не выключен (предел 0 у всех Telegram- и WhatsApp-аккаунтов).
const phoneOffer = computed(() => phoneLimitHint(statusAccounts.value));
const newAddress = computed<NewAddress | null>(() => {
  const address = parseNewAddress(query.value);
  if (address?.kind === 'tg_phone' && phoneOffer.value !== null && !phoneOffer.value.enabled) return null;
  return address;
});
const phoneTyped = computed(() => parseNewAddress(query.value)?.kind === 'tg_phone' || target.value?.type === 'new' && target.value.address.kind === 'tg_phone');
const showList = computed(
  () => channelHits.value.length > 0 || personHits.value.length > 0 || newAddress.value !== null,
);

const from = computed(() => fromChoice(target.value, statusAccounts.value));
const isMail = computed(() => needsSubject(target.value));
const group = computed(() => (isGroupTarget(target.value) ? target.value : null));
const groupName = computed(() => (group.value?.type === 'mattermost' ? 'Mattermost' : 'Slack'));
const groupMax = computed(() => (group.value === null ? 0 : groupMaxPeople(group.value.type)));
const slackFull = computed(() => group.value !== null && group.value.people.length >= groupMax.value);

const request = computed(() =>
  buildChannelNew({
    target: target.value,
    accountId: chosenAccount.value,
    project: chosenProject.value,
    subject: subject.value,
  }),
);
const canSubmit = computed(() => !busy.value && request.value !== null);
// Подсказка предела под полем — по выбранному «Откуда» (у Telegram и WhatsApp пределы свои).
const phoneHint = computed(() => phoneLimitHint(statusAccounts.value, chosenAccount.value));

watch(
  from,
  (choice) => {
    if (chosenAccount.value === null || !choice.accounts.some((a) => a.id === chosenAccount.value)) {
      chosenAccount.value = choice.preset;
    }
    if (choice.fixed) chosenAccount.value = choice.preset;
  },
  { immediate: true },
);
watch([target, chosenAccount], () => {
  error.value = null;
});

// Поиск: пауза после ввода; ответ, пришедший после более нового запроса (или после очистки поля), отбрасывается.
let timer: ReturnType<typeof setTimeout> | null = null;
let seq = 0;

function clearTimer(): void {
  if (timer !== null) clearTimeout(timer);
  timer = null;
}

watch(query, (value) => {
  clearTimer();
  seq += 1;
  const mine = seq;
  const q = value.trim();
  searchFailed.value = false;
  if (q.length < RECIPIENT_MIN_CHARS) {
    hits.value = [];
    return;
  }
  timer = setTimeout(() => {
    timer = null;
    searchRecipients(q).then(
      (result) => {
        if (mine !== seq) return;
        hits.value = Array.isArray(result) ? result : [];
      },
      () => {
        if (mine !== seq) return;
        hits.value = [];
        searchFailed.value = true;
      },
    );
  }, RECIPIENT_DEBOUNCE_MS);
});

function resetQuery(): void {
  query.value = '';
  hits.value = [];
  toInput.value?.focus();
}

function sourceName(source: string): string {
  if (source === 'telegram') return 'Telegram';
  if (source === 'slack') return 'Slack';
  if (source === 'mattermost') return 'Mattermost';
  if (source === 'whatsapp') return 'WhatsApp';
  if (source === 'mail') return 'Почта';
  return source;
}

function projectName(projectId: number | null): string {
  if (projectId === null) return 'Неразобранное';
  return store.state.projects.find((p) => p.id === projectId)?.name ?? `проект #${projectId}`;
}

function accountName(accountId: number): string {
  const account = statusAccounts.value.find((a) => a.id === accountId);
  return account ? accountLabel(account) : `аккаунт #${accountId}`;
}

async function chooseChannel(hit: RecipientChannel): Promise<void> {
  emit('done', hit.channel_id);
  await store.openRecipientChannel(hit.project_id, hit.channel_id).catch(() => undefined);
}

function choosePerson(hit: RecipientPerson): void {
  target.value = pickPerson(target.value, hit);
  resetQuery();
}

function chooseNew(address: NewAddress): void {
  target.value = { type: 'new', address };
  resetQuery();
}

function clearTarget(): void {
  target.value = null;
  toInput.value?.focus();
}

function removeSlack(id: string): void {
  target.value = removeSlackPerson(target.value, id);
}

onMounted(() => {
  if (store.state.status === null) {
    void store.refreshStatus().catch(() => undefined);
  }
  toInput.value?.focus();
});

onBeforeUnmount(() => {
  clearTimer();
  seq += 1;
});

async function submit(): Promise<void> {
  const body = request.value;
  if (!canSubmit.value || body === null) return;
  busy.value = true;
  error.value = null;
  try {
    const channel = await store.createNewChannel(body);
    emit('done', channel.id);
  } catch (err) {
    error.value =
      err instanceof ApiError && [409, 422, 429].includes(err.status) && err.message
        ? err.message
        : 'Беседа не создана — проверьте соединение и попробуйте ещё раз.';
  } finally {
    busy.value = false;
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.preventDefault();
    emit('cancel');
  }
}
</script>

<template>
  <NewGroupForm
    v-if="mode === 'group'"
    :project-id="projectId"
    :accounts="waAccounts"
    @done="emit('done', $event)"
    @cancel="emit('cancel')"
    @mode="mode = $event"
  />
  <form
    v-else
    class="new-message-form"
    :class="{ 'new-message-form--phone': phone }"
    role="dialog"
    aria-label="Новое сообщение"
    @submit.prevent="submit"
    @keydown="onKeydown"
  >
    <div
      v-if="waAccounts.length > 0"
      class="group-mode-switch"
    >
      <button
        type="button"
        aria-pressed="true"
      >
        Сообщение
      </button>
      <button
        type="button"
        aria-pressed="false"
        @click="mode = 'group'"
      >
        Новая группа
      </button>
    </div>
    <h3 class="new-message-title">
      Новое сообщение
      <!-- Ф80-1 F3 (Т11): на телефоне форма во весь экран — закрыть можно и крестиком в шапке. -->
      <button
        v-if="phone"
        type="button"
        class="new-message-close"
        aria-label="Закрыть"
        @click="emit('cancel')"
      >
        ×
      </button>
    </h3>
    <div class="new-message-field">
      <label for="new-message-to">Кому</label>
      <div
        v-if="target !== null"
        class="new-message-chips"
      >
        <template v-if="isGroupTarget(target)">
          <span
            v-for="person in target.people"
            :key="person.id"
            class="new-message-chip"
            :data-slack-id="person.id"
          >
            {{ person.name }}
            <button
              type="button"
              class="new-message-chip-remove"
              :aria-label="`Убрать ${person.name}`"
              @click="removeSlack(person.id)"
            >×</button>
          </span>
        </template>
        <span
          v-else
          class="new-message-chip"
        >
          <template v-if="target.type === 'person'">
            {{ personLabel(target.person) }}
          </template>
          <template v-else-if="target.address.kind === 'telegram'">
            Telegram: @{{ target.address.username }}
          </template>
          <template v-else-if="target.address.kind === 'tg_phone'">
            Telegram: {{ target.address.phone }}
          </template>
          <template v-else>
            {{ target.address.address }}
          </template>
          <button
            type="button"
            class="new-message-chip-remove"
            aria-label="Убрать адресата"
            @click="clearTarget"
          >×</button>
        </span>
      </div>
      <input
        id="new-message-to"
        ref="toInput"
        v-model="query"
        type="text"
        class="new-message-to"
        :placeholder="group !== null ? `добавить человека ${groupName}` : 'имя, беседа, @username, номер или адрес почты'"
        maxlength="300"
        autocomplete="off"
      >
      <p
        v-if="slackFull"
        class="new-message-hint"
      >
        В одной беседе {{ groupName }} — не больше {{ groupMax }} человек.
      </p>
      <p
        v-if="phoneTyped && phoneHint !== null"
        class="new-message-hint new-message-phone-hint"
      >
        <template v-if="phoneHint.enabled">
          по номеру телефона — до {{ phoneHint.daily }} в сутки
        </template>
        <template v-else>
          поиск по номеру выключен
        </template>
      </p>
      <p
        v-if="searchFailed"
        class="new-message-hint new-message-search-error"
      >
        Поиск не ответил — попробуйте ещё раз.
      </p>
      <div
        v-if="showList"
        class="new-message-suggest"
      >
        <template v-if="channelHits.length > 0">
          <p class="new-message-suggest-title">
            Беседы
          </p>
          <button
            v-for="hit in channelHits"
            :key="`c${hit.channel_id}`"
            type="button"
            class="new-message-suggest-item new-message-suggest-channel"
            :data-channel-id="hit.channel_id"
            @click="chooseChannel(hit)"
          >
            <span>{{ hit.title ?? '(без названия)' }}</span>
            <span class="new-message-suggest-meta">{{ sourceName(hit.source) }} · {{ projectName(hit.project_id) }}</span>
          </button>
        </template>
        <template v-if="personHits.length > 0">
          <p class="new-message-suggest-title">
            Люди
          </p>
          <button
            v-for="(hit, index) in personHits"
            :key="`p${index}`"
            type="button"
            class="new-message-suggest-item new-message-suggest-person"
            :data-account-id="hit.account_id"
            :disabled="slackFull && group !== null && hit.source === group.type && group.accountId === hit.account_id"
            @click="choosePerson(hit)"
          >
            <span>{{ personLabel(hit) }}</span>
            <span class="new-message-suggest-meta">{{ accountName(hit.account_id) }}</span>
          </button>
        </template>
        <template v-if="newAddress !== null">
          <p class="new-message-suggest-title">
            Новый адресат
          </p>
          <button
            type="button"
            class="new-message-suggest-item new-message-suggest-new"
            @click="chooseNew(newAddress)"
          >
            <template v-if="newAddress.kind === 'telegram'">
              Telegram: написать @{{ newAddress.username }}
            </template>
            <template v-else-if="newAddress.kind === 'tg_phone'">
              Telegram: написать по номеру {{ newAddress.phone }}
            </template>
            <template v-else>
              Письмо на {{ newAddress.address }}
            </template>
          </button>
        </template>
      </div>
    </div>
    <label
      v-if="target !== null"
      class="new-message-field"
    >
      <span>Откуда</span>
      <span
        v-if="from.fixed"
        class="new-message-from-fixed"
      >{{ from.accounts[0] ? accountLabel(from.accounts[0]) : '' }}</span>
      <select
        v-else
        v-model="chosenAccount"
        class="new-message-account"
      >
        <option
          v-if="from.preset === null"
          :value="null"
          disabled
        >
          — выберите —
        </option>
        <option
          v-for="item in from.accounts"
          :key="item.id"
          :value="item.id"
        >
          {{ accountLabel(item) }}
        </option>
      </select>
    </label>
    <p
      v-if="target !== null && !from.fixed && from.accounts.length === 0"
      class="new-message-hint"
    >
      Нет аккаунта, с которого можно написать этому адресату.
    </p>
    <label
      v-if="isMail"
      class="new-message-field"
    >
      <span>Тема</span>
      <input
        v-model="subject"
        type="text"
        class="new-message-subject"
        maxlength="200"
        required
      >
    </label>
    <label class="new-message-field">
      <span>Проект</span>
      <select
        v-model="chosenProject"
        class="new-message-project"
      >
        <option value="">
          По правилам
        </option>
        <option
          v-for="p in projects"
          :key="p.id"
          :value="String(p.id)"
        >
          {{ p.name ?? '(без названия)' }}
        </option>
      </select>
    </label>
    <div class="new-message-actions">
      <button
        type="submit"
        class="new-message-submit"
        :disabled="!canSubmit"
      >
        Создать
      </button>
      <button
        type="button"
        class="new-message-cancel"
        @click="emit('cancel')"
      >
        Отмена
      </button>
    </div>
    <p
      v-if="error"
      class="new-message-error"
      role="alert"
    >
      {{ error }}
    </p>
  </form>
</template>
