// Ф56-5б (`docs/architecture.md` и 6): состояние панели терминала и ЕДИНСТВЕННЫЙ поток
// `GET /term/stream` на вкладку браузера (К2) — кадры всех терминалов с `term_id`. Поток закрывается, когда вкладка
// скрыта (как `/sessions/live`), и открывается при показе с вектором позиций (`?pos=`), так что ничего не теряется.
// Кадры, пришедшие раньше, чем смонтирован `TerminalPane` терминала, ждут в очереди (`pending`).
// Здесь же: порядок ввода (К11 — один `input` в полёте, остальное копится и склеивается), размер (К10 — шлёт только
// видимая вкладка с фокусом), `runInTerminal` (кнопка «▶ в терминал», К8 — 409 `term_busy` -> вопрос -> повтор с
// `force`), автораскрытие на подсказку пароля (К14 — без кражи фокуса). Содержимого терминала нигде не логируем.
import { reactive } from 'vue';

import * as api from '../api/client';
import type { TermInfo, TermOutFrame, TermRunRecord, TermRunSource, TermSnapshotFrame } from '../api/types';
import { b64ToBytes, bytesToB64 } from '../util/base64';
import { layout } from '../util/layout';
import { readUiId, writeUiId } from '../util/uiPrefs';

export const TERM_LIMIT = 4;
export const TERM_HEIGHT_KEY = 'hub.term.height';
export const TERM_HEIGHT_DEFAULT = 320;
export const TERM_HEIGHT_MIN = 140;
const INPUT_COALESCE_MS = 10;
const INPUT_CHUNK = 60_000;
const RECONNECT_MS = 1500;
const RECONNECT_MAX_MS = 15_000;
const PENDING_CAP_BYTES = 512 * 1024;
const TAIL_BYTES = 200;
/** Подсказка пароля в последних 200 байтах вывода (раздел 3 спеки). */
export const PASSWORD_PROMPT = /\[sudo\] password for|^Password:|пароль/im;

export interface TermSourceLike {
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

/** Получатель кадров одного терминала — `TerminalPane`. */
export interface PaneSink {
  snapshot(frame: TermSnapshotFrame): void;
  out(bytes: Uint8Array): void;
  size(cols: number, rows: number): void;
  closed(): void;
}

type PaneEvent =
  | { kind: 'snapshot'; frame: TermSnapshotFrame }
  | { kind: 'out'; bytes: Uint8Array }
  | { kind: 'size'; cols: number; rows: number }
  | { kind: 'closed' };

export interface BusyAsk {
  termId: number;
  text: string;
  source: TermRunSource;
}

export const termState = reactive({
  open: false,
  collapsed: false,
  fullscreen: false,
  height: readUiId(TERM_HEIGHT_KEY) ?? TERM_HEIGHT_DEFAULT,
  terms: [] as TermInfo[],
  active: null as number | null,
  error: null as string | null,
  busyAsk: null as BusyAsk | null,
  runs: null as TermRunRecord[] | null,
  /** Счётчик просьб «поставить фокус в терминал»; `force` — по клику владельца (кнопка), иначе не отбирать фокус у поля. */
  focusReq: { n: 0, force: false },
  connected: false,
  /** Почему нет потока (подсказка в панели); `null` — ещё подключаемся или поток жив. */
  streamNote: null as string | null,
});

// --- ошибки ---------------------------------------------------------------------------------------------------

const ERROR_TEXT: Record<string, string> = {
  term_limit: `не больше ${TERM_LIMIT} терминалов`,
  term_failed: 'терминал не запустился',
  term_unavailable: 'ядро без терминала',
  term_not_found: 'терминала уже нет',
  empty: 'нечего вставлять',
  too_long: 'слишком длинный текст (до 16 000 знаков)',
  control_chars: 'в тексте управляющие символы — не вставляю',
  force_forbidden: 'повторить так нельзя',
};

export function termErrorText(err: unknown): string {
  if (err instanceof api.ApiError) return ERROR_TEXT[err.message] ?? `ошибка ${err.status}`;
  return 'нет связи с ядром';
}

// --- кадры потока ---------------------------------------------------------------------------------------------

let createSource: (url: string) => TermSourceLike | null = (url) =>
  typeof EventSource !== 'undefined' ? (new EventSource(url) as unknown as TermSourceLike) : null;

/** Для тестов: подмена `EventSource`. */
export function setTermSourceFactory(factory: ((url: string) => TermSourceLike | null) | null): void {
  createSource =
    factory ?? ((url) => (typeof EventSource !== 'undefined' ? (new EventSource(url) as unknown as TermSourceLike) : null));
}

const positions = new Map<number, string>();
const sinks = new Map<number, PaneSink>();
const pending = new Map<number, { events: PaneEvent[]; bytes: number }>();
const tails = new Map<number, Uint8Array>();
let source: TermSourceLike | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = RECONNECT_MS;
let probeSeq = 0;
let visibilityListener: (() => void) | null = null;

function vector(): string {
  return [...positions.entries()].map(([id, pos]) => `${id}@${pos}`).join(',');
}

function deliver(id: number, event: PaneEvent): void {
  const sink = sinks.get(id);
  if (sink !== undefined) {
    callSink(sink, event);
    return;
  }
  const queue = pending.get(id) ?? { events: [], bytes: 0 };
  pending.set(id, queue);
  queue.events.push(event);
  queue.bytes += event.kind === 'out' ? event.bytes.length : event.kind === 'snapshot' ? event.frame.data_b64.length : 0;
  if (queue.bytes > PENDING_CAP_BYTES) {
    // Панель терминала так и не смонтировалась — не копим: забыть позицию, ядро пришлёт снимок заново.
    pending.delete(id);
    positions.delete(id);
    reopenStream();
  }
}

function callSink(sink: PaneSink, event: PaneEvent): void {
  if (event.kind === 'snapshot') sink.snapshot(event.frame);
  else if (event.kind === 'out') sink.out(event.bytes);
  else if (event.kind === 'size') sink.size(event.cols, event.rows);
  else sink.closed();
}

/** Панель терминала подписывается на его кадры; накопленное до монтирования отдаётся сразу. Возвращает снятие. */
export function registerSink(id: number, sink: PaneSink): () => void {
  sinks.set(id, sink);
  const queue = pending.get(id);
  if (queue !== undefined) {
    pending.delete(id);
    for (const event of queue.events) callSink(sink, event);
  }
  return () => {
    if (sinks.get(id) === sink) sinks.delete(id);
  };
}

function parse(ev: MessageEvent): Record<string, unknown> | null {
  try {
    const data: unknown = JSON.parse(String(ev.data));
    return data !== null && typeof data === 'object' ? (data as Record<string, unknown>) : null;
  } catch {
    return null; // содержимое кадра не журналируем даже при ошибке разбора
  }
}

function knownTerm(id: number): boolean {
  return termState.terms.some((t) => t.id === id);
}

function onSnapshot(ev: MessageEvent): void {
  const f = parse(ev) as unknown as TermSnapshotFrame | null;
  if (f === null || typeof f.term_id !== 'number' || typeof f.pos !== 'string') return;
  positions.set(f.term_id, f.pos);
  tails.delete(f.term_id);
  if (!knownTerm(f.term_id)) void refreshTerms();
  deliver(f.term_id, { kind: 'snapshot', frame: f });
}

function onOut(ev: MessageEvent): void {
  const f = parse(ev) as unknown as TermOutFrame | null;
  if (f === null || typeof f.term_id !== 'number' || typeof f.pos !== 'string' || typeof f.data_b64 !== 'string') return;
  positions.set(f.term_id, f.pos);
  const bytes = b64ToBytes(f.data_b64);
  deliver(f.term_id, { kind: 'out', bytes });
  watchPassword(f.term_id, bytes);
}

function onSize(ev: MessageEvent): void {
  const f = parse(ev);
  if (f === null || typeof f.term_id !== 'number' || typeof f.cols !== 'number' || typeof f.rows !== 'number') return;
  const info = termState.terms.find((t) => t.id === f.term_id);
  if (info !== undefined) {
    info.cols = f.cols;
    info.rows = f.rows;
  }
  lastSentSize.set(f.term_id, `${f.cols}x${f.rows}`);
  deliver(f.term_id, { kind: 'size', cols: f.cols, rows: f.rows });
}

function onClosed(ev: MessageEvent): void {
  const f = parse(ev);
  if (f === null || typeof f.term_id !== 'number') return;
  forget(f.term_id);
  deliver(f.term_id, { kind: 'closed' });
  pending.delete(f.term_id);
}

function forget(id: number): void {
  positions.delete(id);
  tails.delete(id);
  inputQueues.delete(id);
  lastSentSize.delete(id);
  termState.terms = termState.terms.filter((t) => t.id !== id);
  if (termState.active === id) termState.active = termState.terms[0]?.id ?? null;
}

/** К14: подсказка пароля в свёрнутой панели -> раскрыть и попросить фокус (поле ввода его не теряет). */
function watchPassword(id: number, bytes: Uint8Array): void {
  const prev = tails.get(id) ?? new Uint8Array(0);
  const joined = new Uint8Array(prev.length + bytes.length);
  joined.set(prev);
  joined.set(bytes, prev.length);
  const tail = joined.length > TAIL_BYTES ? joined.subarray(joined.length - TAIL_BYTES) : joined;
  tails.set(id, tail.slice());
  if (!termState.open || !termState.collapsed) return;
  if (!PASSWORD_PROMPT.test(new TextDecoder().decode(tail))) return;
  tails.delete(id);
  termState.collapsed = false;
  termState.active = id;
  requestFocus(false);
}

export function requestFocus(force: boolean): void {
  termState.focusReq = { n: termState.focusReq.n + 1, force };
}

function wantStream(): boolean {
  return termState.open && (typeof document === 'undefined' || document.visibilityState !== 'hidden');
}

function syncStream(): void {
  if (!wantStream()) {
    closeSource();
    return;
  }
  if (source === null && reconnectTimer === null) openSource();
}

/** Причина по статусу ответа ручки потока (`EventSource` статус не показывает). */
export function streamStatusText(status: number): string {
  if (status === 429) return 'слишком много открытых окон терминала — закрой лишние вкладки хаба';
  if (status === 401 || status === 403) return 'вход устарел — обнови страницу';
  if (status === 421) return 'адрес не принят ядром — открой хаб по обычному адресу';
  if (status === 404 || status === 503) return 'ядро без терминала';
  return `ошибка ${status}`;
}

/** После обрыва потока узнать статус тем же адресом (заголовки приходят сразу; тело не читаем — сразу abort). */
async function probeStream(seq: number): Promise<void> {
  if (typeof fetch === 'undefined') return;
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  try {
    const res = await fetch(api.termStreamUrl(''), {
      credentials: 'same-origin',
      headers: { Accept: 'text/event-stream' },
      signal: ctrl?.signal,
    });
    ctrl?.abort();
    if (seq !== probeSeq || termState.connected) return;
    if (!res.ok) termState.streamNote = streamStatusText(res.status);
  } catch {
    if (seq === probeSeq && !termState.connected) termState.streamNote = 'нет связи с ядром';
  }
}

function openSource(): void {
  const next = createSource(api.termStreamUrl(vector()));
  if (next === null) return;
  source = next;
  next.addEventListener('open', () => {
    if (source !== next) return;
    termState.connected = true;
    termState.streamNote = null;
    reconnectDelay = RECONNECT_MS;
  });
  next.addEventListener('snapshot', onSnapshot);
  next.addEventListener('out', onOut);
  next.addEventListener('size', onSize);
  next.addEventListener('closed', onClosed);
  next.addEventListener('error', () => {
    if (source !== next) return;
    next.close();
    source = null;
    termState.connected = false;
    if (wantStream() && reconnectTimer === null) {
      const delay = reconnectDelay;
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      termState.streamNote = `нет связи с терминалом (повтор через ${Math.round(delay / 1000)} с)`;
      void probeStream(++probeSeq);
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        syncStream();
      }, delay);
    }
  });
}

function closeSource(): void {
  if (source !== null) {
    source.close();
    source = null;
  }
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  termState.connected = false;
  termState.streamNote = null;
  reconnectDelay = RECONNECT_MS;
}

function reopenStream(): void {
  closeSource();
  syncStream();
}

// --- панель -----------------------------------------------------------------------------------------------------

let creating: Promise<void> | null = null;

export async function refreshTerms(): Promise<void> {
  try {
    const list = await api.listTerms();
    termState.terms = [...list].sort((a, b) => a.id - b.id);
    if (termState.active === null || !termState.terms.some((t) => t.id === termState.active)) {
      termState.active = termState.terms[0]?.id ?? null;
    }
  } catch (err) {
    termState.error = termErrorText(err);
  }
}

/** Открыть панель (раскрытой). Терминалов нет — первый создаётся сам (раздел 2.5 спеки). */
export async function openTerminal(): Promise<void> {
  const wasOpen = termState.open;
  termState.open = true;
  termState.collapsed = false;
  termState.error = null;
  if (!wasOpen) {
    if (visibilityListener === null && typeof document !== 'undefined') {
      visibilityListener = () => syncStream();
      document.addEventListener('visibilitychange', visibilityListener);
    }
    syncStream();
  }
  await refreshTerms();
  if (termState.terms.length === 0) await newTerminal();
  requestFocus(true);
}

/** Кнопка «Терминал»: закрыта — открыть; свёрнута — развернуть; раскрыта — свернуть в полосу. */
export function toggleTerminal(): void {
  if (!termState.open) void openTerminal();
  else if (termState.collapsed) expandPanel();
  else collapsePanel();
}

/** Скрыть панель совсем: поток закрыт, позиции забыты; терминалы в tmux живут, при открытии придут снимки. */
export function closePanel(): void {
  termState.open = false;
  termState.collapsed = false;
  termState.fullscreen = false;
  termState.busyAsk = null;
  termState.runs = null;
  closeSource();
  if (visibilityListener !== null) {
    document.removeEventListener('visibilitychange', visibilityListener);
    visibilityListener = null;
  }
  positions.clear();
  pending.clear();
  tails.clear();
  inputQueues.clear();
  lastSentSize.clear();
}

export function collapsePanel(): void {
  termState.collapsed = true;
}

export function expandPanel(): void {
  termState.collapsed = false;
  requestFocus(true);
}

export function setHeight(px: number): void {
  const max = typeof window !== 'undefined' ? Math.max(TERM_HEIGHT_MIN, window.innerHeight - 80) : 2000;
  termState.height = Math.min(Math.max(Math.round(px), TERM_HEIGHT_MIN), max);
}

export function saveHeight(): void {
  writeUiId(TERM_HEIGHT_KEY, termState.height);
}

export async function newTerminal(): Promise<void> {
  if (creating !== null) return creating;
  creating = (async () => {
    try {
      const info = await api.createTerm();
      if (!termState.terms.some((t) => t.id === info.id)) termState.terms = [...termState.terms, info];
      termState.active = info.id;
      termState.error = null;
    } catch (err) {
      termState.error = termErrorText(err);
    } finally {
      creating = null;
    }
  })();
  return creating;
}

export async function closeTerminal(id: number): Promise<void> {
  try {
    await api.closeTerm(id);
  } catch (err) {
    if (!(err instanceof api.ApiError && err.status === 404)) {
      termState.error = termErrorText(err);
      return;
    }
  }
  deliver(id, { kind: 'closed' });
  pending.delete(id);
  forget(id);
}

export function selectTerminal(id: number): void {
  termState.active = id;
  requestFocus(true);
}

// --- ввод и размер ---------------------------------------------------------------------------------------------

const inputQueues = new Map<number, Uint8Array[]>();
const inputInflight = new Set<number>();
const inputTimers = new Map<number, ReturnType<typeof setTimeout>>();

/** К11: ввод склеивается 10 мс; в полёте не больше одного `input` на терминал, остальное ждёт и склеивается. */
export function sendTermInput(id: number, bytes: Uint8Array): void {
  if (bytes.length === 0) return;
  const queue = inputQueues.get(id) ?? [];
  inputQueues.set(id, queue);
  queue.push(bytes);
  if (inputInflight.has(id) || inputTimers.has(id)) return;
  inputTimers.set(
    id,
    setTimeout(() => {
      inputTimers.delete(id);
      void flushInput(id);
    }, INPUT_COALESCE_MS),
  );
}

async function flushInput(id: number): Promise<void> {
  if (inputInflight.has(id)) return;
  const queue = inputQueues.get(id);
  if (queue === undefined || queue.length === 0) return;
  let total = 0;
  for (const part of queue) total += part.length;
  const all = new Uint8Array(total);
  let at = 0;
  for (const part of queue) {
    all.set(part, at);
    at += part.length;
  }
  const chunk = all.subarray(0, INPUT_CHUNK);
  const rest = all.subarray(INPUT_CHUNK);
  inputQueues.set(id, rest.length > 0 ? [rest.slice()] : []);
  inputInflight.add(id);
  try {
    await api.termInput(id, bytesToB64(chunk));
  } catch (err) {
    if (err instanceof api.ApiError && err.status === 404) void refreshTerms();
    else termState.error = termErrorText(err);
  } finally {
    inputInflight.delete(id);
    if ((inputQueues.get(id)?.length ?? 0) > 0) void flushInput(id);
  }
}

const lastSentSize = new Map<number, string>();

/** К10: размер шлёт только видимая вкладка с фокусом; повтор того же размера не посылаем. На телефоне панель
 * во весь экран и зритель один — `hasFocus()` мобильный Chrome отдаёт ложно (адресная строка, шторка), хватает видимости. */
export function canReportSize(): boolean {
  if (typeof document === 'undefined' || document.visibilityState !== 'visible') return false;
  return layout.mode === 'phone' || document.hasFocus();
}

export function reportSize(id: number, cols: number, rows: number): void {
  if (!canReportSize()) return;
  const c = Math.min(Math.max(Math.round(cols), 20), 500);
  const r = Math.min(Math.max(Math.round(rows), 5), 200);
  const key = `${c}x${r}`;
  if (lastSentSize.get(id) === key) return;
  lastSentSize.set(id, key);
  api.termResize(id, c, r).catch((err: unknown) => {
    lastSentSize.delete(id);
    if (err instanceof api.ApiError && err.status === 404) void refreshTerms();
  });
}

// --- «▶ в терминал» ---------------------------------------------------------------------------------------------

export interface RunResult {
  ok: boolean;
  /** 409 `term_busy`: вопрос «вставить всё равно?» показан в панели. */
  busy?: boolean;
}

/** Открыть панель, взять активный терминал (нет — создать) и вставить текст блока кода с Enter. */
export async function runInTerminal(text: string, source: TermRunSource = { kind: 'ui_block' }, force = false, termId?: number): Promise<RunResult> {
  if (!termState.open || termState.collapsed) await openTerminal();
  if (termState.terms.length === 0) await newTerminal();
  const id = termId ?? termState.active ?? termState.terms[0]?.id;
  if (id === undefined) return { ok: false };
  termState.active = id;
  termState.error = null;
  try {
    await api.termRun(id, text, source, force);
    termState.busyAsk = null;
    requestFocus(true);
    return { ok: true };
  } catch (err) {
    if (err instanceof api.ApiError && err.status === 409 && err.message === 'term_busy') {
      termState.busyAsk = { termId: id, text, source };
      return { ok: false, busy: true };
    }
    if (err instanceof api.ApiError && err.status === 404) void refreshTerms();
    termState.error = termErrorText(err);
    return { ok: false };
  }
}

export async function confirmBusyRun(): Promise<void> {
  const ask = termState.busyAsk;
  if (ask === null) return;
  termState.busyAsk = null;
  await runInTerminal(ask.text, ask.source, true, ask.termId);
}

export function dismissBusyRun(): void {
  termState.busyAsk = null;
}

export async function loadRuns(): Promise<void> {
  try {
    termState.runs = await api.listTermRuns(50);
  } catch (err) {
    termState.error = termErrorText(err);
  }
}

export function closeRuns(): void {
  termState.runs = null;
}

/** Только для тестов: вернуть модуль в исходное состояние. */
export function resetTerminal(): void {
  closePanel();
  sinks.clear();
  inputInflight.clear();
  for (const timer of inputTimers.values()) clearTimeout(timer);
  inputTimers.clear();
  termState.terms = [];
  termState.active = null;
  termState.error = null;
  termState.height = TERM_HEIGHT_DEFAULT;
  termState.focusReq = { n: 0, force: false };
  creating = null;
}
