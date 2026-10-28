<script setup lang="ts">
import { onMounted } from 'vue';

import LoginView from './components/LoginView.vue';
import MainApp from './components/MainApp.vue';
import SessionExpiredView from './components/SessionExpiredView.vue';
import { store } from './store/instance';

onMounted(() => {
  void store.bootstrap();
});
</script>

<template>
  <div class="app-root">
    <p
      v-if="store.state.screen === 'loading'"
      class="loading-screen"
    >
      Загрузка…
    </p>
    <LoginView v-else-if="store.state.screen === 'login'" />
    <SessionExpiredView v-else-if="store.state.screen === 'session-expired'" />
    <MainApp v-else />
  </div>
</template>
