<script setup lang="ts">
// Ф56-5б (`docs/architecture.md`): панель терминала снизу поверх всего — один хозяин на
// приложение (монтируется в `MainApp.vue`, как `WriteModalHost`). Вкладки терминалов (до 4), «+», «×» с подтверждением,
// высота тянется (запоминается), свернуть в полосу, «⤢ во весь экран», журнал запусков. Телефон (< 600 px) — во весь
// экран, над клавиатурой ряд клавиш и поле «строка» (xterm там — только показ и прокрутка).
// Сам терминал — `TerminalPane.vue`, подгружается лениво (xterm не попадает в основной пакет).
import { type Component, computed, defineAsyncComponent, defineComponent, h, onBeforeUnmount, ref, shallowRef, watch } from 'vue';
import DictateButton from './DictateButton.vue';

import {
  closePanel,
  closeRuns,
  closeTerminal,
  collapsePanel,
  confirmBusyRun,
  dismissBusyRun,
  expandPanel,
  loadRuns,
  newTerminal,
  refreshTerms,
  saveHeight,
  selectTerminal,
  sendTermInput,
  setHeight,
  TERM_LIMIT,
  termState,
} from '../store/terminal';
import { textToBytes } from '../util/base64';
import { layout } from '../util/layout';
import { paneLoader } from '../util/paneLoader';

// Чанк с xterm мог не загрузиться (VPN, телефон): без обработки остаётся чёрный фон. Показываем состояние и повторяем.
const PaneLoading = defineComponent({
  render: () => h('p', { class: 'term-notice', 'data-test': 'term-pane-loading' }, 'загружаю терминал…'),
});
// «повторить» создаёт новый async-компонент: у старого результат ошибки закэширован.
function makePane() {
  return defineAsyncComponent({
    loader: () => paneLoader.load() as Promise<Component>,
    loadingComponent: PaneLoading,
    errorComponent: defineComponent({
      setup() {
        return () =>
          h('p', { class: 'term-notice term-notice--error', role: 'alert', 'data-test': 'term-pane-error' }, [
            'терминал не загрузился ',
            h('button', { type: 'button', class: 'term-btn', 'data-test': 'term-pane-retry', onClick: () => { TerminalPane.value = makePane(); } }, 'повторить'),
          ]);
      },
    }),
    delay: 200,
    timeout: 20000,
    onError(_error, retry, fail, attempts) {
      if (attempts <= 2) retry();
      else fail();
    },
  });
}
const TerminalPane = shallowRef(makePane());

const REFRESH_MS = 10_000;
const phone = computed(() => layout.mode === 'phone');
const confirmClose = ref<number | null>(null);
const line = ref('');

const KEYS: { label: string; seq: string; title: string }[] = [
  { label: 'Esc', seq: '\x1b', title: 'Escape' },
  { label: 'Tab', seq: '\t', title: 'Tab' },
  { label: 'Ctrl-C', seq: '\x03', title: 'Прервать' },
  { label: 'Ctrl-D', seq: '\x04', title: 'Конец ввода' },
  { label: '↑', seq: '\x1b[A', title: 'Вверх' },
  { label: '↓', seq: '\x1b[B', title: 'Вниз' },
  { label: '←', seq: '\x1b[D', title: 'Влево' },
  { label: '→', seq: '\x1b[C', title: 'Вправо' },
];

const panelClass = computed(() => ({
  'term-panel--collapsed': termState.collapsed,
  'term-panel--full': termState.fullscreen || phone.value,
  'term-panel--phone': phone.value,
}));

const hasTerms = computed(() => termState.terms.length > 0);
const canAdd = computed(() => termState.terms.filter((t) => t.owner !== 'agent').length < TERM_LIMIT);

function tabLabel(id: number): string {
  const t = termState.terms.find((x) => x.id === id);
  return (t?.title ?? '') === '' ? `#${id}` : t!.title;
}

function askClose(id: number): void {
  confirmClose.value = id;
}

async function doClose(): Promise<void> {
  const id = confirmClose.value;
  confirmClose.value = null;
  if (id !== null) await closeTerminal(id);
}

function sendKey(seq: string): void {
  if (termState.active !== null) sendTermInput(termState.active, textToBytes(seq));
}

function sendLine(): void {
  if (termState.active === null) return;
  sendTermInput(termState.active, textToBytes(line.value + '\r'));
  line.value = '';
}

function toggleRuns(): void {
  if (termState.runs === null) void loadRuns();
  else closeRuns();
}

function time(ts: number): string {
  return new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// Высота: тянется за верхнюю кромку (мышь/палец), запоминается после отпускания.
function startDrag(event: PointerEvent): void {
  event.preventDefault();
  const move = (e: PointerEvent): void => setHeight(window.innerHeight - e.clientY);
  const up = (): void => {
    document.removeEventListener('pointermove', move);
    document.removeEventListener('pointerup', up);
    saveHeight();
  };
  document.addEventListener('pointermove', move);
  document.addEventListener('pointerup', up);
}

// Ф80-1 B3: высота открытой панели — в `--term-h` на `:root`: правые панели кончаются над терминалом, а не под ним.
// Во весь экран и на телефоне панели закрыты терминалом целиком — отступ не нужен.
const panelEl = ref<HTMLElement | null>(null);
let termObserver: ResizeObserver | null = null;
function setTermHeight(px: number): void {
  document.documentElement.style.setProperty('--term-h', `${px}px`);
}
watch(panelEl, (el) => {
  termObserver?.disconnect();
  termObserver = null;
  if (el === null) {
    setTermHeight(0);
    return;
  }
  const update = (): void => setTermHeight(phone.value || termState.fullscreen ? 0 : el.offsetHeight);
  update();
  if (typeof ResizeObserver !== 'undefined') {
    termObserver = new ResizeObserver(update);
    termObserver.observe(el);
  }
});
onBeforeUnmount(() => {
  termObserver?.disconnect();
  setTermHeight(0);
});

// Названия вкладок (`команда в папке`) меняются сами — перечитываем список, пока панель открыта.
let ticker: ReturnType<typeof setInterval> | null = null;
watch(
  () => termState.open,
  (open) => {
    if (open && ticker === null) {
      ticker = setInterval(() => {
        if (document.visibilityState === 'visible') void refreshTerms();
      }, REFRESH_MS);
    } else if (!open && ticker !== null) {
      clearInterval(ticker);
      ticker = null;
    }
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  if (ticker !== null) clearInterval(ticker);
});
</script>

<template>
  <section
    v-if="termState.open"
    ref="panelEl"
    class="term-panel"
    :class="panelClass"
    :style="termState.collapsed || termState.fullscreen || phone ? undefined : { height: `${termState.height}px` }"
    aria-label="Терминал"
    data-test="term-panel"
  >
    <div
      v-if="!termState.collapsed && !termState.fullscreen && !phone"
      class="term-grip"
      role="separator"
      aria-label="Потяни, чтобы изменить высоту"
      data-test="term-grip"
      @pointerdown="startDrag"
    />
    <header class="term-bar">
      <nav
        class="term-tabs"
        aria-label="Терминалы"
      >
        <span
          v-for="t in termState.terms"
          :key="t.id"
          class="term-tab"
          :class="{ 'term-tab--active': t.id === termState.active }"
        >
          <button
            type="button"
            class="term-tab-name"
            :aria-current="t.id === termState.active ? 'true' : undefined"
            :data-term-tab="t.id"
            :title="t.command"
            @click="selectTerminal(t.id)"
          >
            <span
              v-if="t.owner === 'agent'"
              title="создан ключом агента"
            >⚙ </span>{{ tabLabel(t.id) }}
          </button>
          <button
            type="button"
            class="term-tab-close"
            :aria-label="`Закрыть терминал ${t.id}`"
            :data-term-close="t.id"
            @click="askClose(t.id)"
          >
            ×
          </button>
        </span>
        <button
          type="button"
          class="term-add"
          title="Новый терминал"
          aria-label="Новый терминал"
          data-test="term-add"
          :disabled="!canAdd"
          @click="newTerminal()"
        >
          +
        </button>
      </nav>
      <span class="term-actions">
        <button
          type="button"
          class="term-btn"
          title="Журнал запусков (кнопка «в терминал» и агент)"
          data-test="term-runs"
          @click="toggleRuns"
        >
          журнал
        </button>
        <button
          v-if="termState.collapsed"
          type="button"
          class="term-btn"
          title="Развернуть"
          aria-label="Развернуть"
          data-test="term-expand"
          @click="expandPanel()"
        >
          ▲
        </button>
        <button
          v-else
          type="button"
          class="term-btn"
          title="Свернуть в полосу"
          aria-label="Свернуть"
          data-test="term-collapse"
          @click="collapsePanel()"
        >
          ▼
        </button>
        <button
          v-if="!phone"
          type="button"
          class="term-btn"
          title="Во весь экран"
          aria-label="Во весь экран"
          data-test="term-full"
          :aria-pressed="termState.fullscreen"
          @click="termState.fullscreen = !termState.fullscreen"
        >
          ⤢
        </button>
        <button
          type="button"
          class="term-btn"
          title="Скрыть панель (терминалы продолжают работать)"
          aria-label="Закрыть панель"
          data-test="term-hide"
          @click="closePanel()"
        >
          ✕
        </button>
      </span>
    </header>

    <div
      v-show="!termState.collapsed"
      class="term-body"
    >
      <p
        v-if="confirmClose !== null"
        class="term-notice term-notice--ask"
        role="alertdialog"
        data-test="term-close-ask"
      >
        Закрыть терминал — оболочка и всё в ней завершится.
        <button
          type="button"
          class="term-btn"
          data-test="term-close-yes"
          @click="doClose"
        >
          Закрыть
        </button>
        <button
          type="button"
          class="term-btn"
          @click="confirmClose = null"
        >
          Отмена
        </button>
      </p>
      <p
        v-if="termState.busyAsk !== null"
        class="term-notice term-notice--ask"
        role="alertdialog"
        data-test="term-busy-ask"
      >
        Терминал занят — вставить всё равно?
        <button
          type="button"
          class="term-btn"
          data-test="term-busy-yes"
          @click="confirmBusyRun()"
        >
          Вставить
        </button>
        <button
          type="button"
          class="term-btn"
          data-test="term-busy-no"
          @click="dismissBusyRun()"
        >
          Отмена
        </button>
      </p>
      <p
        v-if="termState.error !== null"
        class="term-notice term-notice--error"
        role="alert"
        data-test="term-error"
      >
        {{ termState.error }}
        <button
          type="button"
          class="link-button"
          aria-label="Скрыть"
          @click="termState.error = null"
        >
          ×
        </button>
      </p>

      <p
        v-if="hasTerms && !termState.connected && termState.error === null"
        class="term-notice term-notice--stream"
        role="status"
        data-test="term-stream-note"
      >
        {{ termState.streamNote ?? 'подключаюсь…' }}
      </p>

      <div class="term-panes">
        <component
          :is="TerminalPane"
          v-for="t in termState.terms"
          v-show="t.id === termState.active"
          :key="t.id"
          :term-id="t.id"
          :active="t.id === termState.active"
          :phone="phone"
        />
        <p
          v-if="!hasTerms"
          class="term-empty"
          data-test="term-empty"
        >
          Терминалов нет.
          <button
            type="button"
            class="term-btn"
            @click="newTerminal()"
          >
            Открыть новый
          </button>
        </p>
      </div>

      <div
        v-if="termState.runs !== null"
        class="term-runs"
        data-test="term-runs-list"
      >
        <header>
          Журнал запусков
          <button
            type="button"
            class="link-button term-runs-close"
            aria-label="Закрыть журнал"
            @click="closeRuns()"
          >
            ×
          </button>
        </header>
        <p v-if="termState.runs.length === 0">
          Запусков не было.
        </p>
        <ol v-else>
          <li
            v-for="r in termState.runs"
            :key="r.id"
          >
            <span class="term-run-meta">{{ time(r.ts) }} · терминал {{ r.term_id }} · {{ r.who === 'ui' ? 'ты' : 'агент' }} · {{ r.source_kind }}</span>
            <code class="term-run-text">{{ r.text }}</code>
          </li>
        </ol>
      </div>

      <div
        v-if="phone && hasTerms"
        class="term-keys"
        data-test="term-keys"
      >
        <div class="term-keys-row">
          <button
            v-for="k in KEYS"
            :key="k.label"
            type="button"
            class="term-key"
            :title="k.title"
            @click="sendKey(k.seq)"
          >
            {{ k.label }}
          </button>
        </div>
        <form
          class="term-line"
          @submit.prevent="sendLine"
        >
          <input
            v-model="line"
            type="text"
            class="term-line-input"
            placeholder="строка"
            aria-label="Строка для терминала"
            autocomplete="off"
            autocapitalize="off"
            autocorrect="off"
            spellcheck="false"
            enterkeyhint="send"
            data-test="term-line"
          >
          <DictateButton />
          <button
            type="submit"
            class="term-key"
            title="Отправить строку и Enter"
            data-test="term-line-send"
          >
            ⏎
          </button>
        </form>
      </div>
    </div>
  </section>
</template>
