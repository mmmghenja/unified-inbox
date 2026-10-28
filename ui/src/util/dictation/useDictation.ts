// Ф59-1: обвязка Web Audio для диктовки (Vue). Захват: AudioContext + `resume()` СИНХРОННО в клике, до первого
// await (iOS считает контекст «разблокированным» только так), `getUserMedia({audio:true})`, ворклет
// `/audio/pcm-capture-worklet.js`, запасной `ScriptProcessor` (встроенные браузеры без AudioWorklet). Узел идёт
// к `destination` через `GainNode(gain=0)` — Safari без пути до вывода кадры не отдаёт. Обрыв дорожки,
// `interrupted` у контекста и уход вкладки в фон — «стоп» с отдачей хвоста. Звук и текст нигде не хранятся.
import { onBeforeUnmount, ref, type Ref } from 'vue';

import { getDictateStatus, postDictate } from '../../api/client';
import {
  createResamplerState,
  createSegmenter,
  floatFrameRms,
  levelFromRms,
  resampleTo16k,
  smoothLevel,
  type ResamplerState,
  type Segmenter,
} from './capture';
import { createDictation, DICTATE_ERROR_TEXT, insertAtCursor, type Dictation } from './core';

export type DictateField = HTMLTextAreaElement | HTMLInputElement;

const WORKLET_URL = '/audio/pcm-capture-worklet.js?v=1';
/** Не чаще этого перерисовываем индикатор громкости — кадры идут 10–40 раз в секунду. */
const LEVEL_UI_INTERVAL_MS = 80;
/** Первый кусок закрывается после 3 с речи — первый текст появляется в поле быстро. */
const FIRST_CHUNK_SPEECH_MS = 3000;

/** Браузер умеет записать микрофон в Web Audio (и это безопасный контекст — иначе `getUserMedia` нет). */
export function dictationSupported(win: Window & typeof globalThis = window): boolean {
  const w = win as unknown as { AudioContext?: unknown; webkitAudioContext?: unknown };
  return (
    win.isSecureContext === true &&
    typeof win.navigator?.mediaDevices?.getUserMedia === 'function' &&
    (typeof w.AudioContext === 'function' || typeof w.webkitAudioContext === 'function')
  );
}

// --- «диктовка включена в ядре» — один запрос на все кнопки страницы ------------------------------------------

/** `null` — ещё не знаем (запрос идёт или не удался); кнопки скрыты, пока не `true`. */
const enabledState = ref<boolean | null>(null);
let enabledRequest: Promise<void> | null = null;

export function useDictateEnabled(): Ref<boolean | null> {
  if (enabledState.value === null && enabledRequest === null) {
    enabledRequest = getDictateStatus()
      .then((r) => {
        enabledState.value = r.enabled === true;
      })
      .catch(() => {
        // сеть/401: решение не принято, при следующей кнопке спросим ещё раз
      })
      .finally(() => {
        enabledRequest = null;
      });
  }
  return enabledState;
}

/** Для тестов: забыть ответ ядра. */
export function resetDictateEnabled(): void {
  enabledState.value = null;
  enabledRequest = null;
}

// --- сама запись ----------------------------------------------------------------------------------------------

type AnyAudioContext = AudioContext;

function audioContextCtor(): (new () => AnyAudioContext) | null {
  const w = window as unknown as {
    AudioContext?: new () => AnyAudioContext;
    webkitAudioContext?: new () => AnyAudioContext;
  };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

export interface DictationView {
  listening: Ref<boolean>;
  pending: Ref<boolean>;
  /** Громкость 0..1. */
  level: Ref<number>;
  /** Секунд записи. */
  elapsed: Ref<number>;
  error: Ref<string | null>;
  start: () => void;
  stop: () => void;
}

/** `getTarget` вызывается в момент нажатия «начать»: поле, в курсор которого пойдёт текст. */
export function useDictation(getTarget: () => DictateField | null): DictationView {
  const listening = ref(false);
  const pending = ref(false);
  const level = ref(0);
  const elapsed = ref(0);
  const error = ref<string | null>(null);

  let engine: Dictation | null = null;
  let ctx: AnyAudioContext | null = null;
  let stream: MediaStream | null = null;
  let node: AudioWorkletNode | ScriptProcessorNode | null = null;
  let gain: GainNode | null = null;
  let segmenter: Segmenter | null = null;
  let resampler: ResamplerState | null = null;
  let cleanupListeners: (() => void) | null = null;
  let ticker: ReturnType<typeof setInterval> | null = null;
  let startedAt = 0;
  let levelValue = 0;
  let levelShownAt = 0;
  /** Номер сеанса: поздний ответ `getUserMedia` узнаёт, что «Стоп» уже нажат. */
  let session = 0;

  function teardownAudio(): void {
    cleanupListeners?.();
    cleanupListeners = null;
    if (ticker !== null) clearInterval(ticker);
    ticker = null;
    try {
      node?.disconnect();
    } catch {
      /* уже отключён */
    }
    try {
      gain?.disconnect();
    } catch {
      /* уже отключён */
    }
    try {
      stream?.getTracks().forEach((t) => t.stop());
    } catch {
      /* дорожка уже остановлена */
    }
    try {
      void ctx?.close();
    } catch {
      /* контекст уже закрыт */
    }
    node = null;
    gain = null;
    stream = null;
    ctx = null;
    segmenter = null;
    resampler = null;
    levelValue = 0;
    level.value = 0;
    elapsed.value = 0;
  }

  /** Отдать хвост (если в нём достаточно речи) и остановить запись — «Стоп», обрыв дорожки, фон. */
  async function stopAndFlush(): Promise<void> {
    session++;
    const current = engine;
    const tail = segmenter?.flush() ?? null;
    teardownAudio();
    listening.value = false;
    if (!current) return;
    const tailBytes =
      tail !== null && tail.length > 0 ? new Uint8Array(tail.buffer, tail.byteOffset, tail.byteLength) : undefined;
    await current.stopAndFlush(tailBytes);
    // Очередь доиграна; движок живёт до следующего старта (или размонтирования), чтобы не потерять ошибку.
  }

  function fail(text: string): void {
    error.value = text;
    engine?.dispose();
    engine = null;
    teardownAudio();
    listening.value = false;
    pending.value = false;
  }

  function start(): void {
    if (listening.value) return;
    error.value = null;
    const target = getTarget();
    if (target === null || !dictationSupported()) {
      error.value = DICTATE_ERROR_TEXT.unsupported;
      return;
    }
    const mySession = ++session;

    const Ctor = audioContextCtor();
    if (Ctor === null) {
      error.value = DICTATE_ERROR_TEXT.unsupported;
      return;
    }
    let context: AnyAudioContext;
    try {
      context = new Ctor();
      void context.resume(); // синхронно в клике, до первого await — правило iOS
    } catch {
      error.value = DICTATE_ERROR_TEXT.unsupported;
      return;
    }
    ctx = context;

    engine?.dispose();
    engine = createDictation(
      {
        send: (bytes, signal) => postDictate(bytes, signal),
        insert: (text) => {
          if (target.isConnected) insertAtCursor(target, text);
        },
      },
      {
        onPendingChange: (p) => {
          pending.value = p;
        },
        onError: (kind) => {
          error.value = DICTATE_ERROR_TEXT[kind];
        },
        onRecovered: () => {
          if (error.value === DICTATE_ERROR_TEXT.retry) error.value = null;
        },
        onDisabled: (kind) => {
          if (kind === 'off') enabledState.value = false; // ключ убрали посреди записи — кнопки уходят
          fail(DICTATE_ERROR_TEXT[kind]);
        },
      },
    );
    listening.value = true;

    void (async () => {
      let media: MediaStream;
      try {
        media = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) {
        const name = e instanceof Error ? e.name : '';
        const denied = name === 'NotAllowedError' || name === 'PermissionDeniedError';
        if (session === mySession) fail(denied ? DICTATE_ERROR_TEXT.mic : DICTATE_ERROR_TEXT.unsupported);
        return;
      }
      // Пока ждали разрешение, могли нажать «Стоп» — запись уже не нужна, микрофон не оставляем открытым.
      if (session !== mySession || engine === null || ctx !== context) {
        media.getTracks().forEach((t) => t.stop());
        return;
      }
      stream = media;
      segmenter = createSegmenter({ firstChunkMinSpeechMs: FIRST_CHUNK_SPEECH_MS });
      resampler = createResamplerState();
      startedAt = Date.now();
      elapsed.value = 0;
      ticker = setInterval(() => {
        elapsed.value = Math.floor((Date.now() - startedAt) / 1000);
      }, 500);

      const source = context.createMediaStreamSource(media);
      const silent = context.createGain();
      silent.gain.value = 0;
      gain = silent;

      const onFrame = (samples: Float32Array, rate: number): void => {
        levelValue = smoothLevel(levelValue, levelFromRms(floatFrameRms(samples)));
        const now = Date.now();
        if (now - levelShownAt >= LEVEL_UI_INTERVAL_MS) {
          levelShownAt = now;
          level.value = levelValue;
        }
        if (resampler === null || segmenter === null) return;
        const pcm = resampleTo16k(samples, rate, resampler);
        if (pcm.length === 0) return;
        // За одну пачку (запасной ScriptProcessor шлёт крупные) может закрыться больше одного куска.
        for (const closed of segmenter.pushAll(pcm)) {
          engine?.pushChunk(new Uint8Array(closed.buffer, closed.byteOffset, closed.byteLength));
        }
      };

      try {
        await context.audioWorklet.addModule(WORKLET_URL);
        const worklet = new AudioWorkletNode(context, 'pcm-capture-processor');
        worklet.port.onmessage = (ev: MessageEvent<{ samples: Float32Array; sampleRate: number }>) => {
          onFrame(ev.data.samples, ev.data.sampleRate);
        };
        node = worklet;
        source.connect(worklet);
        worklet.connect(silent);
      } catch {
        // Встроенные браузеры приложений без AudioWorklet — устаревший, но живой ScriptProcessor.
        const proc = context.createScriptProcessor(4096, 1, 1);
        proc.onaudioprocess = (ev: AudioProcessingEvent) => {
          onFrame(ev.inputBuffer.getChannelData(0).slice(), context.sampleRate);
        };
        node = proc;
        source.connect(proc);
        proc.connect(silent);
      }
      silent.connect(context.destination);

      // Пока шли `await`, могли нажать «Стоп» — тогда teardown уже прошёл, а узлы мы только что создали.
      if (session !== mySession) {
        teardownAudio();
        return;
      }

      const track = media.getTracks()[0] as MediaStreamTrack | undefined;
      const onTrackDown = (): void => void stopAndFlush();
      track?.addEventListener('ended', onTrackDown);
      track?.addEventListener('mute', onTrackDown);
      const onCtxState = (): void => {
        if ((context.state as string) === 'interrupted') void stopAndFlush();
      };
      context.addEventListener('statechange', onCtxState);
      const onVisibility = (): void => {
        if (document.visibilityState === 'hidden') void stopAndFlush();
      };
      document.addEventListener('visibilitychange', onVisibility);
      cleanupListeners = () => {
        track?.removeEventListener('ended', onTrackDown);
        track?.removeEventListener('mute', onTrackDown);
        context.removeEventListener('statechange', onCtxState);
        document.removeEventListener('visibilitychange', onVisibility);
      };
    })();
  }

  onBeforeUnmount(() => {
    session++;
    engine?.dispose();
    engine = null;
    teardownAudio();
  });

  return {
    listening,
    pending,
    level,
    elapsed,
    error,
    start,
    stop: () => void stopAndFlush(),
  };
}
