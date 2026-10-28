<script setup lang="ts">
// Ф56-5б (`docs/architecture.md` и 6): один терминал — xterm + `addon-fit`. Кадры берёт из
// `store/terminal.ts` (один поток на вкладку, К2). Всё, что видит владелец, приходит от tmux: снимок (К6 — режимы
// восстанавливаются escape-последовательностями), затем байты вывода. Ввод — в `sendTermInput` (К11), размер —
// `reportSize` (К10: шлёт только видимая вкладка с фокусом, остальные подгоняются под кадр `size`).
// К9: ответы xterm на запросы программы (DA, DSR, DECRQM, OSC-запросы цвета) подавлены — отвечает только tmux, иначе
// в оболочке оседает мусор вроде `^[[?1;2c`.
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';

import type { TermSnapshotFrame } from '../api/types';
import { canReportSize, registerSink, reportSize, sendTermInput, termState } from '../store/terminal';
import { b64ToBytes, binaryToBytes, textToBytes } from '../util/base64';
import { copyText } from '../util/copy';
import { restoreSequences, silenceReplies } from '../util/xtermGlue';

const props = defineProps<{ termId: number; active: boolean; phone: boolean }>();

const host = ref<HTMLElement | null>(null);
const FIT_DEBOUNCE_MS = 80;
let term: Terminal | null = null;
let fit: FitAddon | null = null;
let observer: ResizeObserver | null = null;
let unregister: (() => void) | null = null;
let fitTimer: ReturnType<typeof setTimeout> | null = null;

function applySnapshot(f: TermSnapshotFrame): void {
  const t = term;
  if (t === null) return;
  t.reset();
  t.resize(f.cols, f.rows);
  const { head, tail } = restoreSequences(f);
  if (head !== '') t.write(head);
  t.write(b64ToBytes(f.data_b64));
  if (tail !== '') t.write(tail);
  scheduleFit();
}

function isTextField(el: Element | null): boolean {
  if (el === null) return false;
  if (el.classList.contains('xterm-helper-textarea')) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (el as HTMLElement).isContentEditable === true;
}

function focusTerminal(force: boolean): void {
  if (term === null || !props.active || props.phone) return;
  if (!force && isTextField(document.activeElement)) return; // К14: не красть фокус у поля ввода
  term.focus();
}

/** Подгонка под контейнер — всегда (серверный размер из снимка окна ноутбука на телефон не годится); отчёт размера
 * серверу — отдельно и только видимой вкладке с фокусом (К10). Ячейка ещё не измерена — повтор до 5 раз с паузой. */
const FIT_RETRY_MS = 200;
const FIT_RETRIES = 5;
function fitAndReport(attempt = 0): void {
  const t = term;
  const f = fit;
  const el = host.value;
  if (t === null || f === null || el === null || !props.active || termState.collapsed) return;
  if (el.clientWidth === 0 || el.clientHeight === 0) return;
  const dims = f.proposeDimensions();
  if (dims === undefined || !Number.isFinite(dims.cols) || !Number.isFinite(dims.rows)) {
    if (attempt < FIT_RETRIES) {
      if (fitTimer !== null) clearTimeout(fitTimer);
      fitTimer = setTimeout(() => {
        fitTimer = null;
        fitAndReport(attempt + 1);
      }, FIT_RETRY_MS);
    }
    return;
  }
  if (dims.cols !== t.cols || dims.rows !== t.rows) t.resize(dims.cols, dims.rows);
  t.scrollToBottom();
  if (canReportSize()) reportSize(props.termId, dims.cols, dims.rows);
}

function scheduleFit(): void {
  if (fitTimer !== null) clearTimeout(fitTimer);
  fitTimer = setTimeout(() => {
    fitTimer = null;
    fitAndReport(0);
  }, FIT_DEBOUNCE_MS);
}

function onWindowFocus(): void {
  scheduleFit();
}

onMounted(() => {
  const el = host.value;
  if (el === null) return;
  const t = new Terminal({
    scrollback: 5000,
    cursorBlink: true,
    fontSize: props.phone ? 12 : 13,
    fontFamily: 'ui-monospace, "DejaVu Sans Mono", Menlo, Consolas, monospace',
    theme: { background: '#111111', foreground: '#e0e0e0' },
    disableStdin: props.phone,
    allowProposedApi: false,
  });
  const f = new FitAddon();
  t.loadAddon(f);
  silenceReplies(t);
  t.open(el);
  if (props.phone && t.textarea) t.textarea.setAttribute('inputmode', 'none'); // только показ: клавиатура не всплывает
  t.onData((data) => sendTermInput(props.termId, textToBytes(data)));
  t.onBinary((data) => sendTermInput(props.termId, binaryToBytes(data)));
  t.attachCustomKeyEventHandler((ev) => {
    // Ctrl+C при выделении — копировать, а не прерывать; без выделения — как обычно.
    if (ev.type === 'keydown' && ev.ctrlKey && !ev.shiftKey && ev.key.toLowerCase() === 'c' && t.hasSelection()) {
      void copyText(t.getSelection());
      return false;
    }
    return true;
  });
  term = t;
  fit = f;
  unregister = registerSink(props.termId, {
    snapshot: applySnapshot,
    out: (bytes) => t.write(bytes),
    size: (cols, rows) => {
      if (cols !== t.cols || rows !== t.rows) t.resize(cols, rows);
    },
    closed: () => undefined,
  });
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => scheduleFit());
    observer.observe(el);
  }
  window.addEventListener('focus', onWindowFocus);
  document.addEventListener('visibilitychange', onWindowFocus);
  scheduleFit();
  focusTerminal(false);
});

onBeforeUnmount(() => {
  if (fitTimer !== null) clearTimeout(fitTimer);
  window.removeEventListener('focus', onWindowFocus);
  document.removeEventListener('visibilitychange', onWindowFocus);
  observer?.disconnect();
  unregister?.();
  term?.dispose();
  term = null;
  fit = null;
});

watch(
  () => props.active,
  (now) => {
    if (!now) return;
    scheduleFit();
    focusTerminal(true);
  },
);
watch(
  () => termState.connected,
  (on) => {
    if (on) scheduleFit();
  },
);
watch(
  () => termState.collapsed,
  (collapsed) => {
    if (!collapsed) scheduleFit();
  },
);
watch(
  () => termState.focusReq.n,
  () => focusTerminal(termState.focusReq.force),
);
</script>

<template>
  <div
    ref="host"
    class="term-pane"
    :data-term-id="termId"
  />
</template>
