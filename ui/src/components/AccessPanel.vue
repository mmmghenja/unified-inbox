<script setup lang="ts">
// Ф10-7 B4 (`docs/architecture.md`): панель «Доступ и входы» — из правого блока `TopBar` и из
// верхней полосы `PhoneShell`.
// - «Доступ с телефона»: на ноутбуке (`/auth/me.remote === false`) — вкл/выкл, адрес(а), задать/сменить пароль,
//   неверных за сутки и блок; с телефона — только строка состояния, без правки (правка — только локальный слушатель).
// - «Входы»: UI-сессии — «этот», ноутбук/телефон, устройство из User-Agent, когда вошёл и когда был; «Выйти» у
//   строки, внизу «Выйти везде, кроме этого» и «Выйти везде» (с подтверждением). Отзыв своей — экран входа.
// Все строки от сервера (адреса, User-Agent) — только текстом.
// Ф14-9 решение 7 (`docs/architecture.md`): строка про https (срок сертификата / http с подсказкой /
// ошибка красным / < 14 дней — предупреждение), адрес не из сертификата — предупреждение, адрес для телефона —
// со схемой, которую реально отдаёт удалённый слушатель.
import { computed, onMounted, ref } from 'vue';

import * as api from '../api/client';
import type { RemoteStatus, UiSession } from '../api/types';
import { store } from '../store/instance';
import {
  CONFIRM_REVOKE_ALL,
  CONFIRM_REVOKE_OTHERS,
  deviceLabel,
  hostsNotInCert,
  newPasswordProblem,
  parseHosts,
  PASSWORD_MAX,
  phoneUrl,
  sessionTime,
  tlsLine,
  viaLabel,
} from '../util/remote';
import { usePanel } from '../util/popup';

const emit = defineEmits<{ close: [] }>();
// Ф80-1 B1: Esc и нажатие мимо закрывают панель (общий механизм `util/popup`).
const panelRoot = ref<HTMLElement | null>(null);
usePanel(() => panelRoot.value, () => emit('close'));

const remote = ref<RemoteStatus | null>(null);
const remoteError = ref<string | null>(null);
const sessions = ref<UiSession[]>([]);
const sessionsError = ref<string | null>(null);
const busy = ref(false);
const notice = ref<string | null>(null);

const isRemoteSession = computed(() => store.state.remoteSession);

const hostsText = ref('');
const pwOpen = ref(false);
const pw1 = ref('');
const pw2 = ref('');
const pwError = ref<string | null>(null);

function applyRemote(status: RemoteStatus): void {
  remote.value = status;
  hostsText.value = status.hosts.join(', ');
}

async function loadRemote(): Promise<void> {
  try {
    applyRemote(await api.getRemote());
    remoteError.value = null;
  } catch {
    remoteError.value = 'Не удалось загрузить состояние доступа.';
  }
}

async function loadSessions(): Promise<void> {
  try {
    sessions.value = await store.listSessions();
    sessionsError.value = null;
  } catch {
    sessionsError.value = 'Не удалось загрузить список входов.';
  }
}

onMounted(() => {
  void loadRemote();
  void loadSessions();
});

async function act(fn: () => Promise<void>, failText: (err: unknown) => string): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  notice.value = null;
  try {
    await fn();
  } catch (err) {
    notice.value = failText(err);
  } finally {
    busy.value = false;
  }
}

function is422(err: unknown): boolean {
  return err instanceof api.ApiError && err.status === 422;
}

async function setEnabled(enabled: boolean): Promise<void> {
  await act(
    async () => applyRemote(await api.putRemote({ enabled })),
    (err) => (is422(err) ? 'Сначала задай пароль и адрес.' : 'Не удалось — попробуй ещё раз.'),
  );
}

async function saveHosts(): Promise<void> {
  const hosts = parseHosts(hostsText.value);
  await act(
    async () => {
      applyRemote(await api.putRemote({ hosts }));
      notice.value = 'Адрес сохранён.';
    },
    (err) =>
      is422(err)
        ? 'Адрес не подходит: нужен вид hub.домен:8787, от одного до пяти.'
        : 'Не удалось сохранить адрес — попробуй ещё раз.',
  );
}

function openPassword(): void {
  pwOpen.value = true;
  pw1.value = '';
  pw2.value = '';
  pwError.value = null;
}

async function savePassword(): Promise<void> {
  const problem = newPasswordProblem(pw1.value, pw2.value);
  if (problem !== null) {
    pwError.value = problem;
    return;
  }
  pwError.value = null;
  await act(
    async () => {
      const { revoked_remote } = await api.setRemotePassword(pw1.value);
      pwOpen.value = false;
      pw1.value = '';
      pw2.value = '';
      notice.value = revoked_remote > 0 ? `Пароль сохранён. Вышло телефонов: ${revoked_remote}.` : 'Пароль сохранён.';
      await loadRemote();
      await loadSessions();
    },
    (err) => (is422(err) ? 'Пароль не подходит: 10–200 символов.' : 'Не удалось сохранить пароль — попробуй ещё раз.'),
  );
}

async function revoke(session: UiSession): Promise<void> {
  await act(
    async () => {
      await store.revokeSession(session);
      if (session.current) return; // экран входа уже показан
      await loadSessions();
    },
    () => 'Не удалось выйти — попробуй ещё раз.',
  );
}

async function revokeAll(keepCurrent: boolean): Promise<void> {
  if (!window.confirm(keepCurrent ? CONFIRM_REVOKE_OTHERS : CONFIRM_REVOKE_ALL)) return;
  await act(
    async () => {
      const n = await store.revokeAllSessions(keepCurrent);
      if (!keepCurrent) return;
      notice.value = `Вышли на устройствах: ${n}.`;
      await loadSessions();
    },
    () => 'Не удалось выйти — попробуй ещё раз.',
  );
}

const tls = computed(() => (remote.value !== null ? tlsLine(remote.value) : null));
const hostsOutsideCert = computed(() => (remote.value !== null ? hostsNotInCert(remote.value) : []));
const phoneUrls = computed(() => {
  const status = remote.value;
  return status !== null ? status.hosts.map((host) => phoneUrl(status, host)).join(', ') : '';
});

function hostsLine(status: RemoteStatus): string {
  return status.hosts.length > 0 ? status.hosts.join(', ') : 'не задан';
}
</script>

<template>
  <div
    ref="panelRoot"
    class="access-panel"
    role="dialog"
    aria-label="Доступ и входы"
  >
    <div class="access-panel-header">
      <strong>Доступ и входы</strong>
      <button
        type="button"
        class="access-panel-close"
        @click="emit('close')"
      >
        Закрыть
      </button>
    </div>

    <p
      v-if="notice !== null"
      class="access-notice"
      role="status"
    >
      {{ notice }}
    </p>

    <section class="access-section access-remote">
      <h2>Доступ с телефона</h2>
      <p
        v-if="remoteError !== null"
        class="access-error"
      >
        {{ remoteError }}
      </p>
      <template v-else-if="remote !== null">
        <p class="access-state">
          <template v-if="remote.enabled">
            Включён, адрес {{ hostsLine(remote) }}
          </template>
          <template v-else>
            Выключен
          </template>
        </p>
        <p
          v-if="tls !== null"
          class="access-tls"
          :class="{
            'access-error': tls.level === 'error',
            'access-warn': tls.level === 'warn',
          }"
          :data-level="tls.level"
        >
          {{ tls.text }}
        </p>
        <p
          v-for="host in hostsOutsideCert"
          :key="host"
          class="access-warn access-tls-host"
        >
          Сертификат не на этот адрес: {{ host }}
        </p>
        <p
          v-if="remote.hosts.length > 0"
          class="access-phone-url"
        >
          С телефона: <code>{{ phoneUrls }}</code>
        </p>
        <template v-if="!isRemoteSession">
          <p
            v-if="!remote.listening"
            class="access-warn"
          >
            Удалённый вход не поднят: ядро не слушает порт для туннеля.
          </p>
          <div class="access-row">
            <button
              v-if="remote.enabled"
              type="button"
              class="access-disable"
              :disabled="busy"
              @click="setEnabled(false)"
            >
              Выключить
            </button>
            <button
              v-else
              type="button"
              class="access-enable"
              :disabled="busy"
              @click="setEnabled(true)"
            >
              Включить
            </button>
          </div>
          <form
            class="access-hosts"
            @submit.prevent="saveHosts"
          >
            <label>
              Адрес (через запятую, до пяти)
              <input
                v-model="hostsText"
                type="text"
                class="access-hosts-input"
                autocomplete="off"
                autocapitalize="off"
                spellcheck="false"
                placeholder="hub.example.com:8787"
              >
            </label>
            <button
              type="submit"
              :disabled="busy"
            >
              Сохранить адрес
            </button>
          </form>
          <div class="access-password">
            <button
              v-if="!pwOpen"
              type="button"
              class="access-password-open"
              :disabled="busy"
              @click="openPassword"
            >
              {{ remote.password_set ? 'Сменить пароль' : 'Задать пароль' }}
            </button>
            <form
              v-else
              class="access-password-form"
              @submit.prevent="savePassword"
            >
              <p
                v-if="remote.password_set"
                class="access-warn"
              >
                Телефоны выйдут — войти на них снова с новым паролем.
              </p>
              <label>
                Новый пароль (не короче 10 символов)
                <input
                  v-model="pw1"
                  type="password"
                  class="access-pw1"
                  autocomplete="new-password"
                  :maxlength="PASSWORD_MAX"
                >
              </label>
              <label>
                Ещё раз
                <input
                  v-model="pw2"
                  type="password"
                  class="access-pw2"
                  autocomplete="new-password"
                  :maxlength="PASSWORD_MAX"
                >
              </label>
              <p
                v-if="pwError !== null"
                class="access-error"
                role="alert"
              >
                {{ pwError }}
              </p>
              <div class="access-row">
                <button
                  type="submit"
                  class="access-password-save"
                  :disabled="busy"
                >
                  Сохранить пароль
                </button>
                <button
                  type="button"
                  @click="pwOpen = false"
                >
                  Отмена
                </button>
              </div>
            </form>
          </div>
          <p class="access-failures">
            Неверных паролей за сутки: {{ remote.failures_24h }}<template v-if="remote.blocked_until !== null">
              · вход паролем закрыт до {{ sessionTime(remote.blocked_until) }}
            </template>
          </p>
        </template>
      </template>
    </section>

    <section class="access-section access-sessions">
      <h2>Входы</h2>
      <p
        v-if="sessionsError !== null"
        class="access-error"
      >
        {{ sessionsError }}
      </p>
      <ul class="access-session-list">
        <li
          v-for="s in sessions"
          :key="s.id"
          class="access-session"
          :class="{ 'access-session--current': s.current }"
          :data-session-id="s.id"
        >
          <span class="access-session-what">
            <strong v-if="s.current">этот · </strong>{{ viaLabel(s.via) }} · {{ deviceLabel(s.user_agent) }}
          </span>
          <span class="access-session-when">
            вошёл {{ sessionTime(s.created_ts) }} · был {{ sessionTime(s.last_used_ts) }}
          </span>
          <button
            type="button"
            class="access-session-revoke"
            :disabled="busy"
            @click="revoke(s)"
          >
            Выйти
          </button>
        </li>
      </ul>
      <div class="access-row">
        <button
          type="button"
          class="access-revoke-others"
          :disabled="busy"
          @click="revokeAll(true)"
        >
          Выйти везде, кроме этого
        </button>
        <button
          type="button"
          class="access-revoke-all"
          :disabled="busy"
          @click="revokeAll(false)"
        >
          Выйти везде
        </button>
      </div>
    </section>
  </div>
</template>
