<script setup lang="ts">
// Ф41-4 (5.1): поля блока «Подписка» — общие для формы новой сессии и правки в окне сессии. Состояние — объект
// `SubForm` снаружи (v-model на его поля); сборка в `want_sub` и проверки — `util/sessionSub.ts`.
import { SUB_KIND_LABEL, type SubForm, type SubKind } from '../util/sessionSub';

defineProps<{ form: SubForm }>();
defineEmits<{ kind: [] }>();

const KINDS: SubKind[] = ['none', 'cards', 'messages', 'direct'];
</script>

<template>
  <!-- Поля формы — общее реактивное состояние родителя (reactive-объект), v-model пишет прямо в него. -->
  <!-- eslint-disable vue/no-mutating-props -->
  <fieldset class="session-sub-fields">
    <legend>Подписка</legend>
    <label class="work-field">
      <span>Что присылать сессии</span>
      <select
        v-model="form.kind"
        class="session-sub-kind"
        @change="$emit('kind')"
      >
        <option
          v-for="k in KINDS"
          :key="k"
          :value="k"
        >
          {{ SUB_KIND_LABEL[k] }}
        </option>
      </select>
    </label>
    <template v-if="form.kind === 'messages'">
      <label class="session-sub-check">
        <input
          v-model="form.personal"
          type="checkbox"
          class="session-sub-personal"
        >
        <span>только личное</span>
      </label>
      <label class="work-field">
        <span>Ключевые слова (через запятую)</span>
        <input
          v-model="form.keywords"
          type="text"
          class="session-sub-keywords"
          maxlength="1000"
        >
      </label>
      <label class="work-field">
        <span>Исключить (через запятую)</span>
        <input
          v-model="form.exclude"
          type="text"
          class="session-sub-exclude"
          maxlength="1000"
        >
      </label>
      <label class="work-field">
        <span>Пачка, мин (пусто — умолчание: с ключами 15, без — сразу)</span>
        <input
          v-model="form.batch"
          type="text"
          inputmode="numeric"
          class="session-sub-batch"
        >
      </label>
      <label class="work-field">
        <span>Аккаунты</span>
        <select
          v-model="form.accountsMode"
          class="session-sub-accounts-mode"
        >
          <option value="all">
            все
          </option>
          <option value="only">
            только
          </option>
          <option value="except">
            все, кроме
          </option>
        </select>
      </label>
      <label
        v-if="form.accountsMode !== 'all'"
        class="work-field"
      >
        <span>Номера аккаунтов (через запятую)</span>
        <input
          v-model="form.accounts"
          type="text"
          class="session-sub-accounts"
        >
      </label>
    </template>
  </fieldset>
</template>
