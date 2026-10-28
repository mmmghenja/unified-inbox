// Ф14-4 D (docs/architecture.md, тесты E8, E9): голосовое из поля ввода. Микрофон — только при
// безопасном контексте + `MediaRecorder` + `getUserMedia`, иначе «Записать» (`accept="audio/*" capture`);
// запись -> «Стоп» (дорожки микрофона гаснут) -> прослушать -> «Отправить» (загрузка с `voice`, `send` с одним
// файлом и пустым текстом, текст поля цел) / «Удалить»; отказ микрофона — текст; 10:00 — стоп сам; смена
// беседы — запись выброшена. Лента: `voice`/`audio` в webm — звук, видео-webm — видео. Клиент: `&voice=1`.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, MediaItem } from '../src/api/types';
import Composer from '../src/components/Composer.vue';
import { parseAttachments } from '../src/render/attachments';
import { displayFor } from '../src/render/media';
import { store } from '../src/store/instance';
import { canRecordVoice, formatVoiceTime, pickVoiceMime, voiceFileName } from '../src/util/voice';

function channel(id: number, source = 'telegram'): Channel {
  return {
    id,
    ext_id: `c${id}`,
    kind: 'dm',
    title: 'Канал',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 1,
    source,
  };
}

class FakeTrack {
  stopped = false;
  stop(): void {
    this.stopped = true;
  }
}

class FakeStream {
  tracks = [new FakeTrack()];
  getTracks(): FakeTrack[] {
    return this.tracks;
  }
}

class FakeRecorder extends EventTarget {
  static supported = new Set<string>(['audio/webm;codecs=opus']);
  static instances: FakeRecorder[] = [];
  static isTypeSupported(type: string): boolean {
    return FakeRecorder.supported.has(type);
  }

  state: 'inactive' | 'recording' = 'inactive';
  mimeType: string;

  constructor(
    public stream: FakeStream,
    opts?: { mimeType?: string },
  ) {
    super();
    this.mimeType = opts?.mimeType ?? 'audio/webm';
    FakeRecorder.instances.push(this);
  }

  start(): void {
    this.state = 'recording';
  }

  stop(): void {
    this.state = 'inactive';
    // Как в браузере: последний кусок и `stop` приходят асинхронно, уже после вызова `stop()`.
    queueMicrotask(() => {
      const data = new Event('dataavailable') as Event & { data: Blob };
      data.data = new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3])], { type: this.mimeType });
      this.dispatchEvent(data);
      this.dispatchEvent(new Event('stop'));
    });
  }
}

let streams: FakeStream[] = [];
let getUserMedia: MockInstance;
let uploadSpy: MockInstance;
let sendSpy: MockInstance;
let revokeSpy: ReturnType<typeof vi.fn>;
let wrapper: VueWrapper | null = null;
let urlCounter = 0;

function setSecure(value: boolean): void {
  Object.defineProperty(window, 'isSecureContext', { value, configurable: true });
}

function installMic(): void {
  setSecure(true);
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  getUserMedia = vi.fn(async () => {
    const s = new FakeStream();
    streams.push(s);
    return s;
  }) as unknown as MockInstance;
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true });
}

function mountComposer(channelId = 1): VueWrapper {
  store.state.sync.channelsById = { 1: channel(1), 2: channel(2) };
  wrapper = mount(Composer, { props: { channelId }, attachTo: document.body });
  return wrapper;
}

/** Ф90-3: «Записать голосовое» — пункт меню 📎; true — пункт есть (меню открыто). */
async function menuHas(w: VueWrapper, selector: string): Promise<boolean> {
  if (!w.find('.composer-attach-menu').exists()) {
    const attach = w.find('.composer-attach');
    if (attach.exists()) await attach.trigger('click');
  }
  return w.find(selector).exists();
}

async function record(w: VueWrapper): Promise<void> {
  await menuHas(w, '.voice-start');
  await w.get('.voice-start').trigger('click');
  await flushPromises();
}

async function stopRec(w: VueWrapper): Promise<void> {
  await w.get('.voice-stop').trigger('click');
  await flushPromises();
}

beforeEach(() => {
  streams = [];
  urlCounter = 0;
  FakeRecorder.instances = [];
  FakeRecorder.supported = new Set(['audio/webm;codecs=opus']);
  revokeSpy = vi.fn();
  Object.defineProperty(URL, 'createObjectURL', {
    value: vi.fn(() => `blob:http://localhost/rec-${++urlCounter}`),
    configurable: true,
  });
  Object.defineProperty(URL, 'revokeObjectURL', { value: revokeSpy, configurable: true });
  uploadSpy = vi.spyOn(store, 'uploadFile').mockImplementation(async (_c: number, _f: Blob, name: string) => ({
    upload_id: 'v1',
    size: 4,
    mime: 'audio/webm',
    name,
    kind: 'voice',
  }));
  sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  setSecure(false);
  Reflect.deleteProperty(navigator, 'mediaDevices');
  store.state.sync.channelsById = {};
});

describe('51 Ф14-4: микрофон или «Записать»', () => {
  it('безопасный контекст + MediaRecorder + getUserMedia — микрофон, «Записать» нет', async () => {
    installMic();
    const w = mountComposer();
    expect(await menuHas(w, '.voice-start')).toBe(true);
    expect(await menuHas(w, '.composer-record')).toBe(false);
    expect(w.find('input[capture]').exists()).toBe(false);
  });

  it.each([
    ['без безопасного контекста', () => setSecure(false)],
    ['без MediaRecorder', () => vi.stubGlobal('MediaRecorder', undefined)],
  ])('%s — «Записать» с capture, микрофона нет', async (_name, spoil) => {
    installMic();
    spoil();
    const w = mountComposer();
    expect(await menuHas(w, '.voice-start')).toBe(false);
    await menuHas(w, '.composer-record');
    const button = w.get('.composer-record');
    expect(button.text()).toBe('Записать');
    const input = w.get('input[capture]');
    expect(input.attributes('accept')).toBe('audio/*');
    // Выбранный файл — обычное вложение (без признака голосового).
    const file = new File(['ID3'], 'rec.m4a', { type: 'audio/mp4' });
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true });
    await input.trigger('change');
    await flushPromises();
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(uploadSpy.mock.calls[0]![2]).toBe('rec.m4a');
    expect(uploadSpy.mock.calls[0]![3]).toBeUndefined();
    expect(w.get('.composer-file-name').text()).toBe('rec.m4a');
  });

  it('источник без файлов — ни микрофона, ни «Записать»', async () => {
    installMic();
    store.state.sync.channelsById = { 1: channel(1, 'pyrus') };
    wrapper = mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
    expect(await menuHas(wrapper, '.voice-start')).toBe(false);
    expect(await menuHas(wrapper, '.composer-record')).toBe(false);
  });
});

describe('51 Ф14-4: запись и отправка', () => {
  it('запись -> Стоп -> прослушать -> Отправить: voice-загрузка, send с одним файлом и пустым текстом', async () => {
    installMic();
    const w = mountComposer();
    await w.get('textarea').setValue('черновик');
    await record(w);
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(w.find('.voice-recording').exists()).toBe(true);
    expect(w.find('.voice-dot').exists()).toBe(true);
    expect(w.get('.voice-time').text()).toBe('0:00');
    expect(streams[0]!.tracks[0]!.stopped).toBe(false);

    await stopRec(w);
    expect(streams[0]!.tracks[0]!.stopped).toBe(true); // индикатор микрофона гаснет сразу
    const audio = w.get('audio.voice-audio');
    expect(audio.attributes('src')).toBe('blob:http://localhost/rec-1');
    expect(audio.attributes('controls')).toBeDefined();

    await w.get('.voice-send').trigger('click');
    await flushPromises();
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    const [channelId, blob, name, opts] = uploadSpy.mock.calls[0]!;
    expect(channelId).toBe(1);
    expect(blob).toBeInstanceOf(Blob);
    expect(name).toBe('voice.webm');
    expect(opts).toEqual({ voice: true });
    expect(sendSpy).toHaveBeenCalledTimes(1);
    const [sendChannel, sendText, key, sendOpts] = sendSpy.mock.calls[0]!;
    expect(sendChannel).toBe(1);
    expect(sendText).toBe('');
    expect(typeof key).toBe('string');
    expect((sendOpts as { files: string[] }).files).toEqual(['v1']);
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('черновик'); // текст поля цел
    expect(revokeSpy).toHaveBeenCalledWith('blob:http://localhost/rec-1');
    expect(w.find('.voice-recorded').exists()).toBe(false);
    expect(await menuHas(w, '.voice-start')).toBe(true);
  });

  it('Firefox (ogg/opus) — имя voice.ogg', async () => {
    installMic();
    FakeRecorder.supported.add('audio/ogg;codecs=opus');
    const w = mountComposer();
    await record(w);
    expect(FakeRecorder.instances[0]!.mimeType).toBe('audio/ogg;codecs=opus');
    await stopRec(w);
    await w.get('.voice-send').trigger('click');
    await flushPromises();
    expect(uploadSpy.mock.calls[0]![2]).toBe('voice.ogg');
  });

  it('Удалить — запись убрана, blob отозван, ничего не отправлено', async () => {
    installMic();
    const w = mountComposer();
    await record(w);
    await stopRec(w);
    await w.get('.voice-remove').trigger('click');
    await flushPromises();
    expect(w.find('.voice-recorded').exists()).toBe(false);
    expect(revokeSpy).toHaveBeenCalledWith('blob:http://localhost/rec-1');
    expect(uploadSpy).not.toHaveBeenCalled();
    expect(sendSpy).not.toHaveBeenCalled();
  });

  it('ошибка отправки — текстом, запись остаётся; повтор — тот же ключ, без второй загрузки', async () => {
    installMic();
    const w = mountComposer();
    await record(w);
    await stopRec(w);
    sendSpy.mockRejectedValueOnce(new api.ApiError(422, 'голосовое — отдельным сообщением', null));
    await w.get('.voice-send').trigger('click');
    await flushPromises();
    expect(w.get('.voice-error').text()).toContain('голосовое — отдельным сообщением');
    expect(w.find('audio.voice-audio').exists()).toBe(true);
    expect(revokeSpy).not.toHaveBeenCalled();
    await w.get('.voice-send').trigger('click');
    await flushPromises();
    expect(uploadSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy).toHaveBeenCalledTimes(2);
    expect(sendSpy.mock.calls[1]![2]).toBe(sendSpy.mock.calls[0]![2]);
    expect(w.find('.voice-recorded').exists()).toBe(false);
  });

  it('отказ микрофона — «нет доступа к микрофону», кнопка остаётся', async () => {
    installMic();
    getUserMedia.mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'));
    const w = mountComposer();
    await record(w);
    const alert = w.get('.voice-error');
    expect(alert.text()).toBe('нет доступа к микрофону');
    expect(alert.attributes('role')).toBe('alert');
    expect(await menuHas(w, '.voice-start')).toBe(true);
    expect(w.find('.voice-recording').exists()).toBe(false);
  });

  it('10:00 — запись останавливается сама, таймер м:сс', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] });
    installMic();
    const w = mountComposer();
    await record(w);
    vi.advanceTimersByTime(65_000);
    await flushPromises();
    expect(w.get('.voice-time').text()).toBe('1:05');
    expect(w.find('.voice-recording').exists()).toBe(true);
    vi.advanceTimersByTime(600_000 - 65_000);
    await flushPromises();
    expect(w.find('.voice-recording').exists()).toBe(false);
    expect(w.find('.voice-recorded').exists()).toBe(true);
    expect(w.get('.voice-time').text()).toBe('10:00');
    expect(streams[0]!.tracks[0]!.stopped).toBe(true);
    expect(FakeRecorder.instances[0]!.state).toBe('inactive');
  });

  it('смена беседы во время записи — запись выброшена, дорожки остановлены', async () => {
    installMic();
    const w = mountComposer();
    await record(w);
    await w.setProps({ channelId: 2 });
    await flushPromises();
    expect(streams[0]!.tracks[0]!.stopped).toBe(true);
    expect(FakeRecorder.instances[0]!.state).toBe('inactive');
    expect(w.find('.voice-recording').exists()).toBe(false);
    expect(w.find('.voice-recorded').exists()).toBe(false); // позднее `stop` рекордера запись не оживил
    expect(await menuHas(w, '.voice-start')).toBe(true);
  });

  it('смена беседы с неотправленной записью — blob отозван, отправки нет', async () => {
    installMic();
    const w = mountComposer();
    await record(w);
    await stopRec(w);
    await w.setProps({ channelId: 2 });
    await flushPromises();
    expect(revokeSpy).toHaveBeenCalledWith('blob:http://localhost/rec-1');
    expect(w.find('.voice-recorded').exists()).toBe(false);
    expect(sendSpy).not.toHaveBeenCalled();
  });

  it('уход со страницы (unmount) во время записи — микрофон освобождён', async () => {
    installMic();
    const w = mountComposer();
    await record(w);
    w.unmount();
    wrapper = null;
    expect(streams[0]!.tracks[0]!.stopped).toBe(true);
  });
});

describe('51 Ф14-4: утилиты', () => {
  it('canRecordVoice/pickVoiceMime/voiceFileName/formatVoiceTime', () => {
    installMic();
    expect(canRecordVoice()).toBe(true);
    setSecure(false);
    expect(canRecordVoice()).toBe(false);
    expect(pickVoiceMime(FakeRecorder as unknown as typeof MediaRecorder)).toBe('audio/webm;codecs=opus');
    FakeRecorder.supported = new Set();
    expect(pickVoiceMime(FakeRecorder as unknown as typeof MediaRecorder)).toBeUndefined();
    expect(voiceFileName('audio/ogg;codecs=opus')).toBe('voice.ogg');
    expect(voiceFileName('audio/webm')).toBe('voice.webm');
    expect(formatVoiceTime(0)).toBe('0:00');
    expect(formatVoiceTime(599)).toBe('9:59');
  });
});

describe('51 Ф14-4: лента (render/media)', () => {
  function media(detected: string): MediaItem {
    return {
      id: 5,
      idx: 0,
      kind: 'voice',
      status: 'ready',
      mime: null,
      size: null,
      name: null,
      width: null,
      height: null,
      duration_s: null,
      detected,
    };
  }

  it('voice/audio + webm -> audio; видео-webm без voice — видео', () => {
    const [voice] = parseAttachments(JSON.stringify([{ type: 'voice' }]));
    const [audio] = parseAttachments(JSON.stringify([{ type: 'audio' }]));
    const [video] = parseAttachments(JSON.stringify([{ type: 'video' }]));
    const [doc] = parseAttachments(JSON.stringify([{ type: 'document' }]));
    expect(displayFor(voice!, media('webm'), 'telegram')).toBe('audio');
    expect(displayFor(audio!, media('webm'), 'telegram')).toBe('audio');
    expect(displayFor(voice!, media('ogg'), 'telegram')).toBe('audio');
    expect(displayFor(video!, media('webm'), 'telegram')).toBe('video');
    expect(displayFor(doc!, media('webm'), 'slack')).toBe('video');
  });
});

describe('51 Ф14-4: API-клиент', () => {
  it('uploadFile с voice — &voice=1; без — как было', async () => {
    const ok = () =>
      new Response(JSON.stringify({ upload_id: 'u', size: 4, mime: 'audio/webm', name: 'voice.webm', kind: 'voice' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    const fetchMock = vi.fn().mockImplementation(async () => ok());
    vi.stubGlobal('fetch', fetchMock);
    const blob = new Blob([new Uint8Array([1])]);
    await api.uploadFile(5, blob, 'voice.webm', { voice: true });
    await api.uploadFile(5, blob, 'a.ogg');
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/v1/uploads?channel_id=5&voice=1');
    expect(fetchMock.mock.calls[1]![0]).toBe('/api/v1/uploads?channel_id=5');
  });
});
