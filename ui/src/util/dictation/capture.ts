// Ф59-1 (docs/architecture.md): чистые функции захвата голоса для диктовки — без DOM.
// На вход — кадры Float32 из ворклета (`public/audio/pcm-capture-worklet.js`), на выход — готовые куски 16 кГц
// Int16LE для `POST /dictate`. Здесь же определение речи/тишины и нарезчик кусков: тишину не шлём (запрос стоит
// денег, как и с речью). Поведение перенесено из Umbrella (`speech-capture.ts`).

/** Частота, которую ждёт SpeechKit v1 (`format=lpcm&sampleRateHertz=16000`). */
export const TARGET_SAMPLE_RATE = 16000;

/** Жёсткий потолок куска: предел ядра — 30 с и 1 МиБ, берём 25 с с запасом. */
export const MAX_CHUNK_SEC = 25;
export const MAX_CHUNK_SAMPLES = MAX_CHUNK_SEC * TARGET_SAMPLE_RATE;
/** Int16 = 2 байта на сэмпл; всегда меньше 1 МиБ. */
export const MAX_CHUNK_BYTES = MAX_CHUNK_SAMPLES * 2;

// 21.10 (Ф79-1): 5 -> 10 с — whisper расставляет знаки по контексту куска, на коротких кусках их почти нет.
const MIN_SPEECH_BEFORE_PAUSE_MS = 10000;
const PAUSE_CLOSE_MS = 600;
/** Хвост с речью короче этого — почти наверняка шум, не отправляем. */
const MIN_TAIL_SPEECH_MS = 500;
const FRAME_MS = 20;
const FRAME_SAMPLES = Math.round((TARGET_SAMPLE_RATE * FRAME_MS) / 1000);
/** Смотрим на самую тихую точку этого окна перед жёсткой обрезкой на 25 с. */
const QUIET_LOOKBACK_MS = 1500;

function floatToInt16(sample: number): number {
  const s = Math.max(-1, Math.min(1, sample));
  return s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
}

/** Дробная позиция ресэмплера между вызовами: пачки ворклета произвольной длины, на стыках сигнал не «спотыкается». */
export interface ResamplerState {
  phase: number;
}

export function createResamplerState(): ResamplerState {
  return { phase: 0 };
}

/** Понижение частоты до 16 кГц усреднением по окну (грубая защита от алиасинга — для речи достаточно). */
export function resampleTo16k(input: Float32Array, inputRate: number, state: ResamplerState): Int16Array {
  if (input.length === 0) return new Int16Array(0);
  if (inputRate === TARGET_SAMPLE_RATE) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) out[i] = floatToInt16(input[i]);
    return out;
  }
  const ratio = inputRate / TARGET_SAMPLE_RATE;
  const halfWindow = Math.max(1, Math.round(ratio));
  const outLength = Math.max(0, Math.floor((input.length - state.phase) / ratio));
  const out = new Int16Array(outLength);
  let pos = state.phase;
  for (let i = 0; i < outLength; i++) {
    const center = Math.round(pos);
    let sum = 0;
    let count = 0;
    for (let k = -halfWindow; k <= halfWindow; k++) {
      const idx = center + k;
      if (idx >= 0 && idx < input.length) {
        sum += input[idx];
        count++;
      }
    }
    out[i] = floatToInt16(count > 0 ? sum / count : 0);
    pos += ratio;
  }
  state.phase = pos - input.length;
  return out;
}

// Порог «речь/тишина» — нижний перцентиль уровня по скользящему окну последних ТИХИХ кадров: непрерывная
// речь порог не поднимает, потому что окно копит только кадры, уже признанные тишиной.
const VAD_WINDOW_FRAMES = 50; // ~1 с тихих кадров
const VAD_PERCENTILE = 0.2;
const VAD_MARGIN = 1.6;
const MIN_ABS_FLOOR = 0.006; // тише шума линии — порог в 0 не уходит

export interface VadState {
  quietLevels: number[];
}

export function createVadState(): VadState {
  return { quietLevels: [] };
}

export function frameRms(frame: Int16Array): number {
  if (frame.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < frame.length; i++) {
    const v = frame[i] / 32768;
    sum += v * v;
  }
  return Math.sqrt(sum / frame.length);
}

function percentileFloor(levels: number[]): number {
  if (levels.length === 0) return 0;
  const sorted = [...levels].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * VAD_PERCENTILE))];
}

/** Классифицирует кадр и обновляет окно — вызывать строго один раз на кадр. */
export function isSpeechFrame(level: number, state: VadState): boolean {
  const floor = percentileFloor(state.quietLevels);
  const speech = level > Math.max(MIN_ABS_FLOOR, floor * VAD_MARGIN);
  if (!speech) {
    state.quietLevels.push(level);
    if (state.quietLevels.length > VAD_WINDOW_FRAMES) state.quietLevels.shift();
  }
  return speech;
}

export interface Segmenter {
  /** Подать пачку 16 кГц Int16; вернуть ВСЕ куски, закрытые за эту пачку, по порядку. */
  pushAll(samples: Int16Array): Int16Array[];
  /** «Стоп»: отдать хвост, только если в нём есть хотя бы 0,5 с речи. */
  flush(): Int16Array | null;
}

export interface SegmenterOptions {
  /** Порог речи перед паузой для ПЕРВОГО куска: первый текст должен появиться быстро. Дальше — обычные 5 с. */
  firstChunkMinSpeechMs?: number;
}

/**
 * Правила закрытия куска: (1) пауза ≥ 600 мс после ≥ 5 с речи — на границе паузы; (2) 25 с без паузы — режем
 * в самой тихой точке последних 1,5 с; (3) `flush` — хвост с ≥ 0,5 с речи.
 */
export function createSegmenter(options: SegmenterOptions = {}): Segmenter {
  const firstChunkMinSpeechMs = options.firstChunkMinSpeechMs ?? MIN_SPEECH_BEFORE_PAUSE_MS;
  let closedCount = 0;
  const vad = createVadState();
  let pending: number[] = [];
  let chunk: number[] = [];
  let speechMs = 0;
  let silenceRunMs = 0;
  let silenceStartIndex: number | null = null;
  let recentFrames: { index: number; rms: number }[] = [];

  function resetChunkState(): void {
    chunk = [];
    speechMs = 0;
    silenceRunMs = 0;
    silenceStartIndex = null;
    recentFrames = [];
  }

  function trimRecent(): void {
    const keepFrames = Math.ceil(QUIET_LOOKBACK_MS / FRAME_MS);
    if (recentFrames.length > keepFrames) recentFrames = recentFrames.slice(recentFrames.length - keepFrames);
  }

  function finalizeUpTo(cutIndex: number): Int16Array {
    const out = Int16Array.from(chunk.slice(0, cutIndex));
    chunk = chunk.slice(cutIndex);
    speechMs = 0;
    silenceRunMs = 0;
    silenceStartIndex = null;
    recentFrames = [];
    closedCount++;
    return out;
  }

  function analyzeFrame(frame: number[]): Int16Array | null {
    const level = frameRms(Int16Array.from(frame));
    const speech = isSpeechFrame(level, vad);
    const indexBefore = chunk.length;
    for (const s of frame) chunk.push(s);
    recentFrames.push({ index: indexBefore, rms: level });
    trimRecent();

    if (speech) {
      speechMs += FRAME_MS;
      silenceRunMs = 0;
      silenceStartIndex = null;
    } else {
      if (silenceStartIndex === null) silenceStartIndex = indexBefore;
      silenceRunMs += FRAME_MS;
    }

    const minSpeechMs = closedCount === 0 ? firstChunkMinSpeechMs : MIN_SPEECH_BEFORE_PAUSE_MS;
    if (speechMs >= minSpeechMs && silenceRunMs >= PAUSE_CLOSE_MS && silenceStartIndex !== null) {
      return finalizeUpTo(silenceStartIndex);
    }
    if (chunk.length >= MAX_CHUNK_SAMPLES) {
      const speechBefore = speechMs;
      let quietest = recentFrames[0];
      for (const f of recentFrames) if (f.rms < quietest.rms) quietest = f;
      const cut = finalizeUpTo(quietest.index > 0 ? quietest.index : chunk.length);
      // 25 секунд тишины/шума — это не кусок: запрос стоил бы денег впустую.
      return speechBefore >= MIN_TAIL_SPEECH_MS ? cut : null;
    }
    return null;
  }

  return {
    pushAll(samples: Int16Array): Int16Array[] {
      for (let i = 0; i < samples.length; i++) pending.push(samples[i]);
      const closed: Int16Array[] = [];
      while (pending.length >= FRAME_SAMPLES) {
        const frame = pending.slice(0, FRAME_SAMPLES);
        pending = pending.slice(FRAME_SAMPLES);
        const out = analyzeFrame(frame);
        if (out) closed.push(out);
      }
      return closed;
    },
    flush(): Int16Array | null {
      const out = speechMs >= MIN_TAIL_SPEECH_MS && chunk.length > 0 ? Int16Array.from(chunk) : null;
      pending = [];
      resetChunkState();
      return out;
    },
  };
}

/** Уровень для индикатора (0..1) из RMS Float32-кадра: от −60 дБ (тишина) до −10 дБ (громкая речь). */
export function levelFromRms(rms: number): number {
  if (!(rms > 0)) return 0;
  return Math.max(0, Math.min(1, (20 * Math.log10(rms) + 60) / 50));
}

export function floatFrameRms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / samples.length);
}

/** Быстрый подъём, плавный спад — индикатор не мельтешит. */
export function smoothLevel(prev: number, next: number): number {
  return next > prev ? prev + (next - prev) * 0.6 : prev + (next - prev) * 0.15;
}

/** Таймер записи «м:сс». */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const sec = total % 60;
  return `${Math.floor(total / 60)}:${sec < 10 ? '0' : ''}${sec}`;
}
