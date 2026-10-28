// Ф59-1 (`docs/architecture.md`): диктовка в полях ввода. Нарезка и определение речи на
// синтетическом сигнале, очередь кусков строго по одному, вставка в курсор с `input`, кнопка скрыта при
// `enabled: false`, ошибки ядра, кнопка стоит во всех полях списка. Ядро (`/dictate`) и Web Audio подделаны.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { defineComponent, h, nextTick, ref } from 'vue';

import * as api from '../src/api/client';
import DictateButton from '../src/components/DictateButton.vue';
import {
  createResamplerState,
  createSegmenter,
  frameRms,
  formatElapsed,
  isSpeechFrame,
  createVadState,
  MAX_CHUNK_BYTES,
  MAX_CHUNK_SAMPLES,
  resampleTo16k,
} from '../src/util/dictation/capture';
import { classifyFailure, createDictation, insertAtCursor, sentenceStart } from '../src/util/dictation/core';
import { dictationSupported, resetDictateEnabled } from '../src/util/dictation/useDictation';

const RATE = 16000;

/** Синус 440 Гц заданной громкости; длительность в секундах. */
function tone(seconds: number, amp = 0.3): Int16Array {
  const out = new Int16Array(Math.round(seconds * RATE));
  for (let i = 0; i < out.length; i++) out[i] = Math.round(Math.sin((2 * Math.PI * 440 * i) / RATE) * amp * 32767);
  return out;
}

function silence(seconds: number): Int16Array {
  return new Int16Array(Math.round(seconds * RATE));
}

function cat(...parts: Int16Array[]): Int16Array {
  const out = new Int16Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

describe('119 capture: ресэмплинг, речь, нарезка', () => {
  it('48 кГц -> 16 кГц: втрое короче, без накопления ошибки между пачками', () => {
    const input = new Float32Array(48000);
    const state = createResamplerState();
    let total = 0;
    for (let i = 0; i < input.length; i += 4800) total += resampleTo16k(input.subarray(i, i + 4800), 48000, state).length;
    expect(Math.abs(total - 16000)).toBeLessThanOrEqual(1);
    expect(resampleTo16k(new Float32Array([0.5, -0.5]), 16000, createResamplerState())).toEqual(
      new Int16Array([16384, -16384]),
    );
  });

  it('кадр речи громче шума, тишина не речь', () => {
    const vad = createVadState();
    expect(isSpeechFrame(frameRms(silence(0.02)), vad)).toBe(false);
    expect(isSpeechFrame(frameRms(tone(0.02)), vad)).toBe(true);
  });

  it('пауза >= 600 мс после речи закрывает кусок; пауза в кусок не попадает', () => {
    const seg = createSegmenter();
    // 21.10: кусок закрывается паузой только после 10 с речи (whisper ставит знаки по контексту куска).
    expect(seg.pushAll(cat(silence(0.5), tone(6), silence(1)))).toHaveLength(0);
    const chunks = createSegmenter().pushAll(cat(silence(0.5), tone(11), silence(1)));
    expect(chunks).toHaveLength(1);
    expect(chunks[0].length).toBeGreaterThan(11 * RATE);
    expect(chunks[0].length).toBeLessThan(11.7 * RATE);
  });

  it('первый кусок режется раньше (3 с речи), если так попросили', () => {
    const seg = createSegmenter({ firstChunkMinSpeechMs: 3000 });
    expect(seg.pushAll(cat(tone(3.5), silence(1)))).toHaveLength(1);
    const slow = createSegmenter();
    expect(slow.pushAll(cat(tone(3.5), silence(1)))).toHaveLength(0);
  });

  it('тишина не уходит: ни кусков, ни хвоста', () => {
    const seg = createSegmenter();
    expect(seg.pushAll(silence(30))).toHaveLength(0);
    expect(seg.flush()).toBeNull();
  });

  it('речь без пауз режется жёстко, кусок не больше предела ядра', () => {
    const seg = createSegmenter();
    const chunks = seg.pushAll(tone(26));
    expect(chunks.length).toBeGreaterThanOrEqual(1);
    for (const c of chunks) {
      expect(c.length).toBeLessThanOrEqual(MAX_CHUNK_SAMPLES + 320);
      expect(c.length * 2).toBeLessThanOrEqual(MAX_CHUNK_BYTES + 640);
      expect(c.length * 2).toBeLessThan(1_048_576);
    }
  });

  it('хвост с речью >= 0,5 с отдаётся при «Стоп», короче — нет', () => {
    const a = createSegmenter();
    a.pushAll(tone(1));
    expect(a.flush()?.length).toBeGreaterThan(0);
    const b = createSegmenter();
    b.pushAll(tone(0.3));
    expect(b.flush()).toBeNull();
  });

  it('таймер м:сс', () => {
    expect(formatElapsed(65_000)).toBe('1:05');
  });
});

describe('119 core: очередь и ошибки', () => {
  const bytes = new Uint8Array([1, 2]);

  it('куски уходят строго по одному, текст — в порядке записи', async () => {
    const resolvers: ((t: string) => void)[] = [];
    let inFlight = 0;
    let maxInFlight = 0;
    const inserted: string[] = [];
    const d = createDictation(
      {
        send: () => {
          inFlight++;
          maxInFlight = Math.max(maxInFlight, inFlight);
          return new Promise((res) => {
            resolvers.push((t) => {
              inFlight--;
              res({ text: t });
            });
          });
        },
        insert: (t) => inserted.push(t),
      },
      { onPendingChange: () => undefined, onError: () => undefined, onDisabled: () => undefined },
    );
    d.pushChunk(bytes);
    d.pushChunk(bytes);
    d.pushChunk(bytes);
    await nextTick();
    expect(resolvers).toHaveLength(1);
    resolvers[0]('раз');
    await flushPromises();
    resolvers[1]('два');
    await flushPromises();
    resolvers[2]('три');
    await flushPromises();
    expect(inserted).toEqual(['раз', 'два', 'три']);
    expect(maxInFlight).toBe(1);
  });

  it('классификация ошибок ядра', () => {
    expect(classifyFailure({ status: 404, detail: 'dictate_off' })).toBe('off');
    expect(classifyFailure({ status: 429, detail: 'dictate_daily' })).toBe('daily');
    expect(classifyFailure({ status: 429, detail: 'rate' })).toBe('retry');
    expect(classifyFailure({ status: 502, detail: 'dictate_key' })).toBe('key');
    expect(classifyFailure({ status: 502, detail: 'x' })).toBe('retry');
    expect(classifyFailure({ status: 504, detail: '' })).toBe('retry');
    expect(classifyFailure({ status: 400, detail: 'bad_audio' })).toBe('skip');
    expect(classifyFailure({ status: 413, detail: '' })).toBe('skip');
    expect(classifyFailure({ status: 401, detail: '' })).toBe('retry');
    expect(classifyFailure(null)).toBe('retry');
  });

  it('суточный лимит: движок выключается, следующие куски не шлются; временный сбой — нет', async () => {
    const send = vi
      .fn()
      .mockRejectedValueOnce(new api.ApiError(429, 'dictate_daily', null))
      .mockResolvedValue({ text: 'x' });
    const onDisabled = vi.fn();
    const d = createDictation({ send, insert: vi.fn() }, { onPendingChange: () => undefined, onError: vi.fn(), onDisabled });
    d.pushChunk(bytes);
    await flushPromises();
    expect(onDisabled).toHaveBeenCalledWith('daily');
    d.pushChunk(bytes);
    await flushPromises();
    expect(send).toHaveBeenCalledTimes(1);

    const send2 = vi.fn().mockRejectedValueOnce(new api.NetworkError(new Error('x'))).mockResolvedValue({ text: 'ok' });
    const onError = vi.fn();
    const onRecovered = vi.fn();
    const insert = vi.fn();
    const e = createDictation(
      { send: send2, insert, retryDelaysMs: [0] },
      { onPendingChange: () => undefined, onError, onDisabled: vi.fn(), onRecovered },
    );
    e.pushChunk(bytes);
    e.pushChunk(bytes);
    await e.stopAndFlush();
    expect(onError).toHaveBeenCalledWith('retry');
    // кусок со сбоем повторён, а не потерян: оба текста вставлены, сбойный — первым
    expect(send2).toHaveBeenCalledTimes(3);
    expect(insert.mock.calls).toEqual([['ok'], ['ok']]);
    expect(onRecovered).toHaveBeenCalledTimes(1);
  });

  it('временный сбой: кусок повторяется с паузами, порядок текста сохраняется, dispose обрывает ожидание', async () => {
    vi.useFakeTimers();
    try {
      let fails = 3;
      const sent: string[] = [];
      const inserted: string[] = [];
      const pending: boolean[] = [];
      const send = vi.fn((b: Uint8Array) => {
        const tag = String(b[0]);
        sent.push(tag);
        if (tag === '1' && fails-- > 0) return Promise.reject(new api.ApiError(502, 'dictate_upstream', null));
        return Promise.resolve({ text: `t${tag}` });
      });
      const d = createDictation(
        { send, insert: (t) => inserted.push(t), retryDelaysMs: [1000, 2000] },
        { onPendingChange: (p) => pending.push(p), onError: vi.fn(), onDisabled: vi.fn() },
      );
      d.pushChunk(new Uint8Array([1]));
      d.pushChunk(new Uint8Array([2]));
      await vi.advanceTimersByTimeAsync(0);
      expect(sent).toEqual(['1']);
      await vi.advanceTimersByTimeAsync(1000); // повтор 1
      expect(sent).toEqual(['1', '1']);
      await vi.advanceTimersByTimeAsync(2000); // повтор 2
      await vi.advanceTimersByTimeAsync(2000); // повтор 3 — пауза дальше последней = последняя
      expect(inserted).toEqual(['t1', 't2']);
      expect(sent).toEqual(['1', '1', '1', '1', '2']);
      expect(pending.at(-1)).toBe(false);

      // связи нет совсем: dispose во время паузы — очередь не висит
      const dead = createDictation(
        { send: () => Promise.reject(new api.NetworkError(new Error('x'))), insert: vi.fn(), retryDelaysMs: [60_000] },
        { onPendingChange: () => undefined, onError: vi.fn(), onDisabled: vi.fn() },
      );
      dead.pushChunk(new Uint8Array([3]));
      const flushed = dead.stopAndFlush();
      await vi.advanceTimersByTimeAsync(10);
      expect(dead.isPending()).toBe(true);
      dead.dispose();
      await flushed;
      expect(dead.isPending()).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('таймаут запроса — временный сбой, кусок повторяется', async () => {
    vi.useFakeTimers();
    try {
      const onError = vi.fn();
      let calls = 0;
      const insert = vi.fn();
      const d = createDictation(
        {
          send: (_b, signal) => {
            calls++;
            if (calls > 1) return Promise.resolve({ text: 'дошло' });
            return new Promise((_res, rej) => {
              signal.addEventListener('abort', () => rej(Object.assign(new Error('abort'), { name: 'AbortError' })));
            });
          },
          insert,
          timeoutMs: 1000,
          retryDelaysMs: [500],
        },
        { onPendingChange: () => undefined, onError, onDisabled: vi.fn() },
      );
      d.pushChunk(bytes);
      await vi.advanceTimersByTimeAsync(1100);
      expect(onError).toHaveBeenCalledWith('retry');
      await vi.advanceTimersByTimeAsync(500);
      expect(insert).toHaveBeenCalledWith('дошло');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('119 вставка в курсор', () => {
  it('в позицию курсора, с пробелами-склейками, поле получает input (v-model подхватывает)', async () => {
    const model = ref('привет мир');
    const wrapper = mount(
      defineComponent({
        setup: () => () => h('textarea', { value: model.value, onInput: (e: Event) => (model.value = (e.target as HTMLTextAreaElement).value) }),
      }),
      { attachTo: document.body },
    );
    const el = wrapper.element as HTMLTextAreaElement;
    el.setSelectionRange(6, 6); // «привет |мир»
    insertAtCursor(el, ' красивый ');
    expect(model.value).toBe('привет красивый мир');
    expect(el.selectionStart).toBe('привет красивый'.length);
    // в конец, без пробела слева
    el.setSelectionRange(el.value.length, el.value.length);
    insertAtCursor(el, 'ещё');
    expect(model.value).toBe('привет красивый мир ещё');
    // в пустое поле — без пробелов
    model.value = '';
    await nextTick();
    insertAtCursor(el, 'начало');
    expect(model.value).toBe('Начало'); // 21.10: начало поля — с заглавной
    // заменяет выделенное
    el.setSelectionRange(0, 3);
    insertAtCursor(el, 'ХХХ');
    expect(model.value).toBe('ХХХ ало');
    wrapper.unmount();
  });

  it('пустой фрагмент ничего не меняет', () => {
    const el = document.createElement('textarea');
    el.value = 'a';
    expect(insertAtCursor(el, '   ')).toBe('');
    expect(el.value).toBe('a');
  });
});

// --- кнопка целиком, подделка Web Audio -----------------------------------------------------------------------

class FakeTrack {
  stopped = false;
  addEventListener(): void {}
  removeEventListener(): void {}
  stop(): void {
    this.stopped = true;
  }
}

class FakeStream {
  track = new FakeTrack();
  getTracks(): FakeTrack[] {
    return [this.track];
  }
}

interface FakeProc {
  onaudioprocess: ((ev: { inputBuffer: { getChannelData: () => Float32Array } }) => void) | null;
  connect(): void;
  disconnect(): void;
}

const audio = vi.hoisted(() => ({ procs: [] as unknown[], streams: [] as unknown[], resumedSync: false }));

class FakeAudioContext {
  sampleRate = 16000;
  state = 'running';
  destination = {};
  // без AudioWorklet: addModule падает -> запасной ScriptProcessor, как во встроенных браузерах
  audioWorklet = { addModule: () => Promise.reject(new Error('no worklet')) };
  resume(): Promise<void> {
    audio.resumedSync = true;
    return Promise.resolve();
  }
  createMediaStreamSource() {
    return { connect() {}, disconnect() {} };
  }
  createGain() {
    return { gain: { value: 1 }, connect() {}, disconnect() {} };
  }
  createScriptProcessor(): FakeProc {
    const p: FakeProc = { onaudioprocess: null, connect() {}, disconnect() {} };
    audio.procs.push(p);
    return p;
  }
  addEventListener(): void {}
  removeEventListener(): void {}
  close(): Promise<void> {
    return Promise.resolve();
  }
}

function floatTone(seconds: number): Float32Array {
  const out = new Float32Array(Math.round(seconds * RATE));
  for (let i = 0; i < out.length; i++) out[i] = Math.sin((2 * Math.PI * 440 * i) / RATE) * 0.3;
  return out;
}

function installAudio(): void {
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: vi.fn(async () => {
        const s = new FakeStream();
        audio.streams.push(s);
        return s;
      }),
    },
  });
  (window as unknown as { AudioContext: unknown }).AudioContext = FakeAudioContext;
}

function uninstallAudio(): void {
  Object.defineProperty(window, 'isSecureContext', { value: false, configurable: true });
  delete (window as unknown as { AudioContext?: unknown }).AudioContext;
}

const Host = defineComponent({
  setup() {
    const text = ref('привет');
    return () =>
      h('div', [
        h('textarea', { id: 'f', value: text.value, onInput: (e: Event) => (text.value = (e.target as HTMLTextAreaElement).value) }),
        h(DictateButton),
      ]);
  },
});

describe('119 кнопка «Надиктовать»', () => {
  beforeEach(() => {
    audio.procs.length = 0;
    audio.streams.length = 0;
    audio.resumedSync = false;
    resetDictateEnabled();
    installAudio();
  });
  afterEach(() => {
    uninstallAudio();
    vi.restoreAllMocks();
  });

  it('браузер без микрофона/безопасного контекста — кнопки нет и ядро не спрашивается', async () => {
    uninstallAudio();
    const status = vi.spyOn(api, 'getDictateStatus');
    expect(dictationSupported()).toBe(false);
    const w = mount(Host);
    await flushPromises();
    expect(w.find('.dictate-btn').exists()).toBe(false);
    expect(status).not.toHaveBeenCalled();
  });

  it('enabled: false — кнопки нет; enabled: true — есть, у неё своя подсказка «Надиктовать»', async () => {
    vi.spyOn(api, 'getDictateStatus').mockResolvedValue({ enabled: false });
    const off = mount(Host);
    await flushPromises();
    expect(off.find('.dictate-btn').exists()).toBe(false);
    off.unmount();

    resetDictateEnabled();
    vi.spyOn(api, 'getDictateStatus').mockResolvedValue({ enabled: true });
    const on = mount(Host);
    await flushPromises();
    const button = on.find('.dictate-btn');
    expect(button.exists()).toBe(true);
    expect(button.attributes('aria-label')).toBe('Надиктовать текст');
    expect(button.text()).toContain('Надиктовать');
    on.unmount();
  });

  it('запись -> куски в ядро -> текст в курсор поля; «Стоп» отдаёт хвост; AudioContext.resume — в самом клике', async () => {
    vi.spyOn(api, 'getDictateStatus').mockResolvedValue({ enabled: true });
    const post = vi.spyOn(api, 'postDictate').mockResolvedValue({ text: 'мир' });
    const w = mount(Host, { attachTo: document.body });
    await flushPromises();
    const field = w.find('#f').element as HTMLTextAreaElement;
    field.setSelectionRange(field.value.length, field.value.length);

    const click = w.find('.dictate-btn').trigger('click');
    expect(audio.resumedSync).toBe(true); // до первого await
    await click;
    await flushPromises();
    expect(w.find('.dictate-pill').exists()).toBe(true);
    expect(w.find('.dictate-stop').exists()).toBe(true);
    expect(w.find('.dictate-status').text()).toContain('Слушаю');

    const proc = audio.procs[0] as FakeProc;
    expect(proc).toBeDefined();
    const speech = floatTone(1.2);
    for (let i = 0; i < speech.length; i += 4096) {
      const part = speech.slice(i, i + 4096);
      proc.onaudioprocess?.({ inputBuffer: { getChannelData: () => part } });
    }
    await w.find('.dictate-stop').trigger('click');
    await flushPromises();

    expect(post).toHaveBeenCalledTimes(1);
    const sent = post.mock.calls[0][0];
    expect(sent.byteLength % 2).toBe(0);
    expect(sent.byteLength).toBeGreaterThan(0.5 * RATE * 2);
    expect(sent.byteLength).toBeLessThan(1_048_576);
    expect(field.value).toBe('привет мир');
    expect(w.find('.dictate-pill').exists()).toBe(false);
    expect((audio.streams[0] as FakeStream).track.stopped).toBe(true);
    w.unmount();
  });

  it('тишина: «Стоп» ничего не шлёт', async () => {
    vi.spyOn(api, 'getDictateStatus').mockResolvedValue({ enabled: true });
    const post = vi.spyOn(api, 'postDictate').mockResolvedValue({ text: 'x' });
    const w = mount(Host, { attachTo: document.body });
    await flushPromises();
    await w.find('.dictate-btn').trigger('click');
    await flushPromises();
    const proc = audio.procs[0] as FakeProc;
    proc.onaudioprocess?.({ inputBuffer: { getChannelData: () => new Float32Array(16000) } });
    await w.find('.dictate-stop').trigger('click');
    await flushPromises();
    expect(post).not.toHaveBeenCalled();
    w.unmount();
  });

  it('ошибки строкой: нет микрофона, не настроено, суточный лимит, плохой ключ, не расслышал', async () => {
    vi.spyOn(api, 'getDictateStatus').mockResolvedValue({ enabled: true });
    // микрофон закрыт
    (navigator.mediaDevices.getUserMedia as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      Object.assign(new Error('x'), { name: 'NotAllowedError' }),
    );
    const w = mount(Host, { attachTo: document.body });
    await flushPromises();
    await w.find('.dictate-btn').trigger('click');
    await flushPromises();
    expect(w.find('.dictate-error').text()).toBe('нет доступа к микрофону');
    expect(w.find('.dictate-pill').exists()).toBe(false);

    const cases: [number, string, string][] = [
      [404, 'dictate_off', 'диктовка не настроена'],
      [429, 'dictate_daily', 'лимит диктовки на сегодня'],
      [502, 'dictate_key', 'ключ диктовки не принят'],
      [504, 'timeout', 'нет связи — повторяю, запись не пропадёт'],
    ];
    for (const [status, detail, text] of cases) {
      resetDictateEnabled();
      audio.procs.length = 0;
      vi.spyOn(api, 'getDictateStatus').mockResolvedValue({ enabled: true });
      vi.spyOn(api, 'postDictate').mockRejectedValue(new api.ApiError(status, detail, null));
      const m = mount(Host, { attachTo: document.body });
      await flushPromises();
      await m.find('.dictate-btn').trigger('click');
      await flushPromises();
      const proc = audio.procs[0] as FakeProc;
      const speech = floatTone(1.2);
      for (let i = 0; i < speech.length; i += 4096) {
        const part = speech.slice(i, i + 4096);
        proc.onaudioprocess?.({ inputBuffer: { getChannelData: () => part } });
      }
      const stop = m.find('.dictate-stop');
      if (stop.exists()) await stop.trigger('click');
      await flushPromises();
      expect(m.find('.dictate-error').text()).toBe(text);
      m.unmount();
    }
    w.unmount();
  });
});

describe('119 кнопка стоит во всех полях ввода (список спеки)', () => {
  const FIELDS: [string, number][] = [
    ['Composer.vue', 1],
    ['SessionWindow.vue', 2], // Ф90-1: поле столбца (одна строка) и прежнее поле окна в списке
    ['AgentDialog.vue', 1],
    ['OwnerTalk.vue', 1],
    ['OwnerCard.vue', 4], // свой ответ, комментарий, текст/поручение, правка черновика
    ['DraftCard.vue', 1],
    ['MessageRow.vue', 1],
    ['NewSessionForm.vue', 1],
    ['TerminalHost.vue', 1],
  ];

  it.each(FIELDS)('%s: <DictateButton> (%i)', (file, count) => {
    const src = readFileSync(resolve(__dirname, '../src/components', file), 'utf8');
    expect(src).toContain("import DictateButton from './DictateButton.vue'");
    expect(src.match(/<DictateButton\b/g)?.length).toBe(count);
  });

  it('кнопка голосового сообщения осталась (пункт меню 📎) и подписана иначе', () => {
    const src = readFileSync(resolve(__dirname, '../src/components/Composer.vue'), 'utf8');
    expect(src).toContain('Записать голосовое');
  });
});

describe('119 заглавная в начале предложения (21.10)', () => {
  it('после конца предложения и в пустом поле — с заглавной, посреди фразы — как пришло', () => {
    expect(sentenceStart('', 'вот как')).toBe('Вот как');
    expect(sentenceStart('Работаю. ', 'у меня')).toBe('У меня');
    expect(sentenceStart('Так и должно быть?', 'качество')).toBe('Качество');
    expect(sentenceStart('у меня все есть', 'а здесь')).toBe('а здесь');
    expect(sentenceStart('', '123 штуки')).toBe('123 штуки');
    expect(sentenceStart('', '')).toBe('');
  });
});
