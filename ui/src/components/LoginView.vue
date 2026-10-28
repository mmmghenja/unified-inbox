<script setup lang="ts">
// UI не может само получить код входа (спека, «Вход UI»): на ноутбуке единственный путь — `hub ui`, который
// открывает эту страницу с кодом во фрагменте (`#code=...`). Если код не пришёл или оказался неверным/просроченным —
// показываем это сообщение, никакой формы ввода кода здесь нет и не будет.
// Ф10-7 B2: страница открыта не на `127.0.0.1`/`localhost` (телефон через VPN) — поле пароля доступа с телефона.
import { isRemoteHostname } from '../util/remote';
import PasswordLoginForm from './PasswordLoginForm.vue';

const remote = isRemoteHostname(window.location.hostname);
</script>

<template>
  <main class="centered-screen">
    <h1>Hub</h1>
    <PasswordLoginForm v-if="remote" />
    <template v-else>
      <p>Нет действительного кода входа. Выполните на этом компьютере команду:</p>
      <p><code>hub ui</code></p>
    </template>
  </main>
</template>
