<script setup lang="ts">
// 401 на любом запросе -> сюда (спека, «Вход UI»): «UI сам код получить не может и не должен».
// Ф10-7 B2: с телефона (адрес не `127.0.0.1`/`localhost`) — поле пароля, на ноутбуке — как было (`hub ui`).
import { isRemoteHostname } from '../util/remote';
import PasswordLoginForm from './PasswordLoginForm.vue';

const remote = isRemoteHostname(window.location.hostname);
</script>

<template>
  <main class="centered-screen">
    <h1>Сессия истекла</h1>
    <PasswordLoginForm v-if="remote" />
    <template v-else>
      <p>Выполните на этом компьютере команду:</p>
      <p><code>hub ui</code></p>
    </template>
  </main>
</template>
