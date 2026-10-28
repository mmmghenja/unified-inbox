<script setup lang="ts">
// Ф62-6 (раздел G): выбор участников группы WhatsApp — тот же поиск «Кому» (`GET /recipients`, от 2 символов,
// пауза 250 мс, устаревший ответ отбрасывается), но только люди и лички WhatsApp выбранного аккаунта; номер
// вручную (`+7…`/`8…`) — отдельной строкой. Имена — чужой текст, только интерполяция.
import { computed, onBeforeUnmount, ref, watch } from 'vue';

import { searchRecipients } from '../api/client';
import type { RecipientHit } from '../api/types';
import { GROUP_MEMBERS_MAX, manualPhone, participantOfHit, type PickedPerson } from '../util/groupOps';
import { RECIPIENT_DEBOUNCE_MS, RECIPIENT_MIN_CHARS } from '../util/newMessage';

const props = defineProps<{ accountId: number | null; modelValue: PickedPerson[]; placeholder?: string }>();
const emit = defineEmits<{ 'update:modelValue': [value: PickedPerson[]] }>();

const query = ref('');
const hits = ref<RecipientHit[]>([]);
const searchFailed = ref(false);

const options = computed(() => {
  if (props.accountId === null) return [];
  const out: PickedPerson[] = [];
  const seen = new Set(props.modelValue.map((p) => p.value));
  for (const hit of hits.value) {
    const pick = participantOfHit(hit, props.accountId);
    if (pick === null || seen.has(pick.value)) continue;
    seen.add(pick.value);
    out.push(pick);
  }
  return out;
});
const typedPhone = computed(() => manualPhone(query.value));
const full = computed(() => props.modelValue.length >= GROUP_MEMBERS_MAX);

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

function add(person: PickedPerson): void {
  if (full.value || props.modelValue.some((p) => p.value === person.value)) return;
  emit('update:modelValue', [...props.modelValue, person]);
  query.value = '';
  hits.value = [];
}

function addTyped(): void {
  const phone = typedPhone.value;
  if (phone !== null) add({ value: phone, label: phone });
}

function remove(value: string): void {
  emit('update:modelValue', props.modelValue.filter((p) => p.value !== value));
}

onBeforeUnmount(() => {
  clearTimer();
  seq += 1;
});
</script>

<template>
  <div class="group-people">
    <div
      v-if="modelValue.length > 0"
      class="group-people-chips"
    >
      <span
        v-for="person in modelValue"
        :key="person.value"
        class="group-people-chip"
      >
        {{ person.label }}
        <button
          type="button"
          class="group-people-remove"
          :aria-label="`Убрать ${person.label}`"
          @click="remove(person.value)"
        >×</button>
      </span>
    </div>
    <input
      v-model="query"
      type="text"
      class="group-people-input"
      :placeholder="placeholder ?? 'имя или номер +7…'"
      maxlength="300"
      autocomplete="off"
      :disabled="full"
      @keydown.enter.prevent="addTyped"
    >
    <p
      v-if="full"
      class="new-message-hint"
    >
      Не больше {{ GROUP_MEMBERS_MAX }} человек за раз.
    </p>
    <p
      v-if="searchFailed"
      class="new-message-hint"
    >
      Поиск не ответил — попробуйте ещё раз.
    </p>
    <div
      v-if="options.length > 0 || typedPhone !== null"
      class="group-people-suggest"
    >
      <button
        v-for="pick in options"
        :key="pick.value"
        type="button"
        class="group-people-option"
        @click="add(pick)"
      >
        {{ pick.label }}
      </button>
      <button
        v-if="typedPhone !== null"
        type="button"
        class="group-people-option group-people-option--phone"
        @click="addTyped"
      >
        Добавить номер {{ typedPhone }}
      </button>
    </div>
  </div>
</template>
