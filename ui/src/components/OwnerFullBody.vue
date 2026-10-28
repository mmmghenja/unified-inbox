<script setup lang="ts">
// Ф56-3: `full` карточки очереди владельца — по видам. Тексты модели (`turn.answer`, `agent_msg.text`) — через
// `ModelText`; всё остальное — чужой текст, только интерполяцией. Общий для карточки (короткое) и окна (длинное).
import { computed, ref } from 'vue';

import type { OwnerItem } from '../api/types';
import { copyText } from '../util/copy';
import { fullFields, humanKindLabel, mainShown, stringify, ownerQuestions } from '../util/owner_channel';
import ModelText from './ModelText.vue';

// `inCard` — тело внутри карточки: `brief` виден рядом, повторять его не нужно (в окне `brief` нет).
const props = defineProps<{ item: OwnerItem; nowMs?: number; inCard?: boolean }>();

const full = computed(() => props.item.full);
const copied = ref(false);

const text = (key: string): string => stringify(full.value[key]);
const isQuestion = computed(() => ownerQuestions(full.value).length > 0);
const questions = computed(() => ownerQuestions(full.value));
const humanKind = computed(() => humanKindLabel(full.value));

// Приёмка, круг 3: подписанные по-русски поля вида (`util/owner_channel.ts::fullFields`); сырых ключей и чисел
// времени в карточке нет. Текст черновика правится в карточке — здесь он только в окне.
const dedup = computed(() => props.inCard === true && props.item.kind !== 'human' && props.item.kind !== 'draft');
const fields = computed(() => fullFields(props.item, props.nowMs ?? Date.now(), dedup.value));
const showMain = computed(() => !dedup.value || mainShown(props.item));

async function copyUrl(): Promise<void> {
  copied.value = await copyText(text('url'));
  if (copied.value) setTimeout(() => (copied.value = false), 1500);
}
</script>

<template>
  <div class="owner-full">
    <template v-if="item.kind === 'turn'">
      <ModelText
        :text="text('answer')"
        :session-id="item.session_id"
        :project-id="item.project_id"
        runnable
      />
      <p
        v-if="full.cut === true"
        class="work-request-truncated"
      >
        показано не всё
      </p>
    </template>
    <template v-else-if="item.kind === 'agent_msg'">
      <ModelText
        :text="text('text')"
        :session-id="item.session_id"
        :project-id="item.project_id"
        runnable
      />
    </template>
    <template v-else-if="isQuestion">
      <div
        v-for="q in questions"
        :key="q.n"
        class="owner-question"
      >
        <p class="owner-question-text">
          {{ q.n }}. {{ q.shown }}
        </p>
        <ul
          v-if="q.options.length > 0"
          class="owner-options"
        >
          <li
            v-for="opt in q.options"
            :key="opt"
          >
            {{ opt }}
          </li>
        </ul>
      </div>
    </template>
    <template v-else-if="item.kind === 'permission'">
      <pre
        v-if="full.input_text && showMain"
        class="role-text-content owner-input"
      >{{ text('input_text') }}</pre>
      <p
        v-if="full.reason"
        class="work-request-reason"
      >
        причина: {{ text('reason') }}
      </p>
      <p
        v-if="full.input_truncated === true"
        class="work-request-truncated"
      >
        показано не всё
      </p>
    </template>
    <template v-else-if="item.kind === 'draft'">
      <pre
        v-if="!inCard"
        class="role-text-content owner-input owner-draft-text"
      >{{ text('text') }}</pre>
    </template>
    <template v-else-if="item.kind === 'human'">
      <p
        v-if="humanKind"
        class="owner-pair"
      >
        <span class="owner-key">что:</span> {{ humanKind }}
      </p>
      <p
        v-if="full.site"
        class="owner-pair"
      >
        <span class="owner-key">сайт:</span> {{ text('site') }}
      </p>
      <p
        v-if="full.url"
        class="owner-pair human-url"
      >
        <span class="owner-key">адрес:</span>
        <span class="human-url-text">{{ text('url') }}</span>
        <button
          type="button"
          class="human-copy"
          @click="copyUrl"
        >
          {{ copied ? 'скопировано' : 'копировать' }}
        </button>
      </p>
      <p
        v-if="full.profile"
        class="owner-pair"
      >
        <span class="owner-key">профиль:</span> {{ text('profile') }}
      </p>
      <p
        v-if="full.tab"
        class="owner-pair"
      >
        <span class="owner-key">вкладка:</span> {{ text('tab') }}
      </p>
      <p
        v-if="full.doing"
        class="owner-pair"
      >
        <span class="owner-key">делаю:</span> {{ text('doing') }}
      </p>
      <p
        v-if="full.need"
        class="owner-pair"
      >
        <span class="owner-key">нужно:</span> {{ text('need') }}
      </p>
    </template>
    <p
      v-for="field in fields"
      :key="field.key"
      class="owner-pair"
      data-test="owner-field"
    >
      <span class="owner-key">{{ field.label }}:</span> {{ field.value }}
    </p>
  </div>
</template>
