<script setup lang="ts">
// Ф14-4 D1 (docs/architecture.md): голосовое из поля ввода — только в безопасном контексте (решает
// `Composer` через `canRecordVoice`). Состояния: покой (кнопка микрофона) -> запись (красная точка, таймер,
// «Стоп»; в 10:00 — стоп сам) -> записано (`<audio>` с `blob:`, длительность, «Отправить» / «Удалить»).
// Дорожки микрофона останавливаются сразу после «Стоп». «Отправить» — загрузка с `voice=1` и отдельное
// сообщение из одного файла с пустым текстом (текст поля ввода не трогается); ошибка — текстом, запись остаётся
// для повтора (тот же ключ идемпотентности, уже загруженный файл не грузится второй раз). Смена беседы, уход со
// страницы — запись выбрасывается без вопроса, микрофон освобождается, `blob:` URL отзывается.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { ApiError } from '../api/client';
import { store } from '../store/instance';
import { newId } from '../util/ids';
import { formatVoiceTime, pickVoiceMime, VOICE_MAX_SECONDS, voiceFileName } from '../util/voice';

const props = defineProps<{
  channelId: number;
  thread?: string | null;
  disabled?: boolean;
}>();

type Phase = 'idle' | 'starting' | 'recording' | 'recorded' | 'sending';

const phase = ref<Phase>('idle');
const error = ref<string | null>(null);
const elapsed = ref(0);
const audioUrl = ref<string | null>(null);

let stream: MediaStream | null = null;
let recorder: MediaRecorder | null = null;
let chunks: Blob[] = [];
let startedAt = 0;
let ticker: ReturnType<typeof setInterval> | null = null;
/** Поколение записи: выброшенная запись не должна ожить по позднему событию `stop` рекордера. */
let generation = 0;
let blob: Blob | null = null;
let fileName = 'voice.webm';
let uploadId: string | null = null;
let sendKey: string | null = null;

const timeLabel = computed(() => formatVoiceTime(elapsed.value));

function stopTracks(): void {
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
}

function stopTicker(): void {
  if (ticker !== null) clearInterval(ticker);
  ticker = null;
}

function revokeUrl(): void {
  if (audioUrl.value !== null) URL.revokeObjectURL(audioUrl.value);
  audioUrl.value = null;
}

/** Выбросить всё: запись (если идёт), микрофон, готовый файл. */
function discard(): void {
  generation += 1;
  stopTicker();
  const active = recorder;
  recorder = null;
  if (active !== null && active.state !== 'inactive') {
    try {
      active.stop();
    } catch {
      // рекордер уже остановлен — нечего делать
    }
  }
  stopTracks();
  revokeUrl();
  chunks = [];
  blob = null;
  uploadId = null;
  sendKey = null;
  elapsed.value = 0;
  phase.value = 'idle';
}

function tick(): void {
  elapsed.value = Math.floor((Date.now() - startedAt) / 1000);
  if (elapsed.value >= VOICE_MAX_SECONDS) {
    elapsed.value = VOICE_MAX_SECONDS;
    stop();
  }
}

async function start(): Promise<void> {
  if (phase.value !== 'idle' || props.disabled) return;
  error.value = null;
  phase.value = 'starting';
  const gen = ++generation;
  let media: MediaStream;
  try {
    media = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    if (gen === generation) {
      phase.value = 'idle';
      error.value = 'нет доступа к микрофону';
    }
    return;
  }
  if (gen !== generation) {
    media.getTracks().forEach((track) => track.stop()); // беседу сменили, пока браузер спрашивал доступ
    return;
  }
  stream = media;
  const mime = pickVoiceMime();
  let rec: MediaRecorder;
  try {
    rec = mime !== undefined ? new MediaRecorder(media, { mimeType: mime }) : new MediaRecorder(media);
  } catch {
    stopTracks();
    phase.value = 'idle';
    error.value = 'запись не поддерживается';
    return;
  }
  recorder = rec;
  chunks = [];
  rec.addEventListener('dataavailable', (event: BlobEvent) => {
    if (gen === generation && event.data && event.data.size > 0) chunks.push(event.data);
  });
  rec.addEventListener('stop', () => {
    if (gen !== generation) return;
    const type = rec.mimeType || mime || 'audio/webm';
    blob = new Blob(chunks, { type });
    chunks = [];
    fileName = voiceFileName(type);
    uploadId = null;
    sendKey = newId();
    audioUrl.value = URL.createObjectURL(blob);
    phase.value = 'recorded';
  });
  startedAt = Date.now();
  elapsed.value = 0;
  rec.start();
  phase.value = 'recording';
  ticker = setInterval(tick, 250);
}

function stop(): void {
  if (phase.value !== 'recording') return;
  stopTicker();
  elapsed.value = Math.min(Math.floor((Date.now() - startedAt) / 1000), VOICE_MAX_SECONDS);
  const active = recorder;
  recorder = null;
  // Индикатор микрофона гаснет сразу: дорожки — до того, как рекордер отдаст последний кусок.
  if (active !== null && active.state !== 'inactive') active.stop();
  stopTracks();
}

function sendErrorText(err: unknown): string {
  if (err instanceof ApiError && err.status === 413) return 'Не удалось отправить: запись больше 20 МиБ';
  if (err instanceof ApiError && (err.status === 409 || err.status === 422) && err.message) {
    return `Не удалось отправить: ${err.message}`;
  }
  return 'Не удалось отправить. Проверьте соединение и попробуйте ещё раз.';
}

async function send(): Promise<void> {
  if (phase.value !== 'recorded' || blob === null) return;
  error.value = null;
  phase.value = 'sending';
  const gen = generation;
  const channelId = props.channelId;
  try {
    if (uploadId === null) {
      const result = await store.uploadFile(channelId, blob, fileName, { voice: true });
      if (gen !== generation) return;
      uploadId = result.upload_id;
    }
    sendKey ??= newId();
    await store.send(channelId, '', sendKey, { thread: props.thread ?? undefined, files: [uploadId] });
    if (gen !== generation) return;
    discard();
  } catch (err) {
    if (gen !== generation) return;
    phase.value = 'recorded';
    error.value = sendErrorText(err);
  }
}

function remove(): void {
  error.value = null;
  discard();
}

// Ф90-3: кнопки «🎤» больше нет — запись начинает меню 📎 в `Composer`.
defineExpose({ start });

watch(
  () => props.channelId,
  () => {
    error.value = null;
    discard();
  },
);

function onPageHide(): void {
  discard();
}

onMounted(() => window.addEventListener('pagehide', onPageHide));
onBeforeUnmount(() => {
  window.removeEventListener('pagehide', onPageHide);
  discard();
});
</script>

<template>
  <span class="voice-recorder">
    <span
      v-if="phase === 'recording'"
      class="voice-recording"
      role="status"
    >
      <span
        class="voice-dot"
        aria-hidden="true"
      />
      <span class="voice-time">{{ timeLabel }}</span>
      <button
        type="button"
        class="voice-stop"
        @click="stop"
      >
        Стоп
      </button>
    </span>
    <span
      v-else-if="phase === 'recorded' || phase === 'sending'"
      class="voice-recorded"
    >
      <audio
        v-if="audioUrl"
        class="voice-audio"
        controls
        :src="audioUrl"
      />
      <span class="voice-time">{{ timeLabel }}</span>
      <button
        type="button"
        class="voice-send"
        :disabled="phase === 'sending'"
        @click="send"
      >
        Отправить
      </button>
      <button
        type="button"
        class="voice-remove"
        :disabled="phase === 'sending'"
        @click="remove"
      >
        Удалить
      </button>
    </span>
    <span
      v-if="error"
      class="voice-error"
      role="alert"
    >{{ error }}</span>
  </span>
</template>
