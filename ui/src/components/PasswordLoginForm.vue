<script setup lang="ts">
// Ф10-7 B2: вход паролем с телефона (страница открыта не на `127.0.0.1`/`localhost`). Форма без `action` и
// с `@submit.prevent` — CSP `form-action 'none'`; `autocomplete="current-password"` — браузер телефона
// подставит сохранённый пароль. Ошибки — строкой под полем (`util/remote.ts` `passwordLoginError`).
import { ref } from 'vue';

import { store } from '../store/instance';

const password = ref('');
const busy = ref(false);
const error = ref<string | null>(null);

async function submit(): Promise<void> {
  if (busy.value || password.value === '') return;
  busy.value = true;
  error.value = null;
  try {
    const problem = await store.loginWithPassword(password.value);
    if (problem !== null) error.value = problem;
    else password.value = '';
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <form
    class="password-login"
    @submit.prevent="submit"
  >
    <label class="password-login-label">
      Пароль
      <input
        v-model="password"
        type="password"
        name="password"
        autocomplete="current-password"
        class="password-login-input"
        :disabled="busy"
        @input="error = null"
      >
    </label>
    <button
      type="submit"
      class="password-login-submit"
      :disabled="busy || password === ''"
    >
      Войти
    </button>
    <p
      v-if="error !== null"
      class="password-login-error"
      role="alert"
    >
      {{ error }}
    </p>
  </form>
</template>
