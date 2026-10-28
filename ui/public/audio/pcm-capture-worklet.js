/* global sampleRate */
// Ф59-1: ворклет диктовки хаба. Только копит сырые кадры Float32 с микрофона и пачками ~100 мс отдаёт их в
// главный поток без обработки: ресэмплинг в 16 кГц, определение речи и нарезка живут в
// `src/util/dictation/capture.ts` (там их проверяют тестами). Файл статический, грузится с того же адреса
// (CSP `script-src 'self'`); версия в URL (`?v=1`) — чтобы браузер не отдавал старый ворклет из кэша.
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = [];
    this._samplesSinceFlush = 0;
    this._flushEverySamples = Math.round(sampleRate * 0.1);
  }

  process(inputs) {
    const input = inputs[0];
    const channel = input && input[0];
    if (channel && channel.length) {
      this._buffer.push(Float32Array.from(channel)); // буфер Web Audio движок переиспользует
      this._samplesSinceFlush += channel.length;
      if (this._samplesSinceFlush >= this._flushEverySamples) this._flush();
    }
    return true;
  }

  _flush() {
    if (this._buffer.length === 0) return;
    let total = 0;
    for (const part of this._buffer) total += part.length;
    const merged = new Float32Array(total);
    let offset = 0;
    for (const part of this._buffer) {
      merged.set(part, offset);
      offset += part.length;
    }
    this._buffer = [];
    this._samplesSinceFlush = 0;
    this.port.postMessage({ samples: merged, sampleRate }, [merged.buffer]);
  }
}

registerProcessor('pcm-capture-processor', PcmCaptureProcessor);
