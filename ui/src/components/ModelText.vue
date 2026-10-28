<script setup lang="ts">
// Ф10-1 B: текст модели (ответ оркестратора, событие `text` сессии, итог задачи) — markdown, оформленный.
// ЕДИНСТВЕННОЕ место с сырым HTML в UI: HTML строит `render/markdown.ts` (markdown-it без сырого HTML + DOMPurify
// со списком тегов/атрибутов). Реплики владельца, заметки, строки инструментов, сообщения людей и черновики
// сюда не идут — у них текстовый вывод.
// «копировать» у блоков кода — делегированием на корне; длинное сворачивается (`collapsible`) по высоте > 320 px.
import { computed, nextTick, onMounted, ref, watch } from 'vue';

import { COPY_LABEL, renderModelMarkdown } from '../render/markdown';
import { openMd } from '../store/mdViewer';
import { runInTerminal } from '../store/terminal';
import { copyText } from '../util/copy';

// Ф56-5б (К3): `runnable` — только у текста модели (ответ сессии, итог задачи, ответ агента): у блоков кода появляется
// «▶ в терминал». Сообщения людей, черновики и живой текст его не включают.
const props = withDefaults(defineProps<{
  text: string;
  collapsible?: boolean;
  runnable?: boolean;
  // Ф72-1: откуда текст — для поиска файла по относительному пути (`openMd`); необязательные.
  sessionId?: number | null;
  projectId?: number | null;
  from?: string;
}>(), {
  collapsible: false,
  runnable: false,
});

const COLLAPSE_PX = 320;
const COPY_RESET_MS = 2000;

const html = computed(() => renderModelMarkdown(props.text, { runnable: props.runnable }));
const body = ref<HTMLElement | null>(null);
const tooTall = ref(false);
const expanded = ref(false);

async function measure(): Promise<void> {
  if (!props.collapsible) return;
  await nextTick();
  const el = body.value;
  tooTall.value = el !== null && el.scrollHeight > COLLAPSE_PX;
}

onMounted(measure);
watch(() => [props.text, props.collapsible], measure);

function onClick(event: MouseEvent): void {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const file = target.closest('a.md-file');
  if (file !== null && body.value !== null && body.value.contains(file)) {
    event.preventDefault();
    const path = file.getAttribute('data-md-path') ?? '';
    if (path !== '') {
      openMd(path, {
        sessionId: props.sessionId ?? undefined,
        projectId: props.projectId ?? undefined,
        from: props.from,
      });
    }
    return;
  }
  const run = target.closest('button.md-run');
  if (run !== null && props.runnable && body.value !== null && body.value.contains(run)) {
    // Показано ровно то, что исполнится: текст блока кода как есть.
    const code = run.parentElement?.querySelector('pre code');
    const text = code?.textContent ?? '';
    if (text.trim() !== '') void runInTerminal(text, { kind: 'ui_block' });
    return;
  }
  const button = target.closest('button.md-copy');
  if (button === null || body.value === null || !body.value.contains(button)) return;
  const code = button.parentElement?.querySelector('pre code');
  const text = code?.textContent ?? '';
  const done = (label: string): void => {
    button.textContent = label;
    setTimeout(() => (button.textContent = COPY_LABEL), COPY_RESET_MS);
  };
  // Ф10-7: `copyText` — и без `navigator.clipboard` (телефон по http), через `execCommand`.
  void copyText(text).then((ok) => done(ok ? 'скопировано' : 'не вышло'));
}
</script>

<template>
  <div
    class="model-text"
    @click="onClick"
  >
    <!-- Безопасно: HTML только из renderModelMarkdown — markdown-it с html:false и DOMPurify по списку тегов и
         атрибутов (href только http(s)); см. render/markdown.ts и тест 31. Другого сырого HTML в UI нет. -->
    <!-- eslint-disable vue/no-v-html -->
    <div
      ref="body"
      class="md"
      :class="{ 'md--collapsed': collapsible && tooTall && !expanded }"
      v-html="html"
    />
    <!-- eslint-enable vue/no-v-html -->
    <button
      v-if="collapsible && tooTall"
      type="button"
      class="link-button md-toggle"
      @click="expanded = !expanded"
    >
      {{ expanded ? 'свернуть' : 'показать полностью' }}
    </button>
  </div>
</template>
