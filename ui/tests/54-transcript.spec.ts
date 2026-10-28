// Ф14-6b (docs/architecture.md, 14-6b «UI», «Мелкое»): расшифровка голосового под плеером
// (`AttachmentList`, блоки аудио и видео): текст по статусам, длинный — свёрнут, `en` — пометка языка,
// «пусто» — «речи не слышно», прочий `problem` — «расшифровка может быть неточной»; кнопки «Расшифровать» /
// «Расшифровать заново» -> `POST /media/{id}/transcribe`, ошибка — текстом рядом. Текст — простым текстом.
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, MediaItem, Message, Transcript } from '../src/api/types';
import MessageRow from '../src/components/MessageRow.vue';
import { parseAttachments } from '../src/render/attachments';
import { foldTranscript, transcribeErrorText, transcriptView } from '../src/render/transcript';
import { store } from '../src/store/instance';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
    project_id: 1,
    ext_id: '1',
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'file',
    text: null,
    entities_json: null,
    attachments_json: JSON.stringify([{ type: 'voice', duration_s: 12 }]),
    forward_json: null,
    mentions_me: false,
    refs_json: null,
    reactions_json: null,
    ts: 1000,
    edited_ts: null,
    deleted: false,
    outbox_id: null,
    outbox_status: null,
    sender: { id: 7, name: 'Аня', handle: null },
    media: [],
    ...overrides,
  };
}

function voice(transcript: Transcript | null, overrides: Partial<MediaItem> = {}): MediaItem {
  return {
    id: 5,
    idx: 0,
    kind: 'voice',
    status: 'ready',
    mime: 'audio/ogg',
    size: 1000,
    name: null,
    width: null,
    height: null,
    duration_s: 12,
    detected: 'ogg',
    transcript,
    ...overrides,
  };
}

function tr(overrides: Partial<Transcript>): Transcript {
  return { status: 'done', text: 'привет, перезвони', lang: 'ru', problem: null, manual: false, ...overrides };
}

function row(media: MediaItem, attachments?: unknown[]) {
  return mount(MessageRow, {
    props: {
      message: msg({
        id: 42,
        media: [media],
        ...(attachments ? { attachments_json: JSON.stringify(attachments) } : {}),
      }),
    },
  });
}

beforeEach(() => {
  store.state.sync.channelsById[1] = { id: 1, source: 'telegram' } as unknown as Channel;
});

afterEach(() => {
  vi.restoreAllMocks();
  delete store.state.sync.channelsById[1];
});

describe('54 transcript: что показать (чистая функция)', () => {
  const [voiceAtt] = parseAttachments(JSON.stringify([{ type: 'voice' }]));
  const [videoAtt] = parseAttachments(JSON.stringify([{ type: 'video' }]));
  const [roundAtt] = parseAttachments(JSON.stringify([{ type: 'video', round: true }]));

  it('кнопки: нет строки — «Расшифровать»; done/failed — «заново»; идёт — без кнопки; видео — никогда', () => {
    expect(transcriptView(voiceAtt!, voice(null)).button).toBe('Расшифровать');
    expect(transcriptView(voiceAtt!, voice(tr({}))).button).toBe('Расшифровать заново');
    expect(transcriptView(voiceAtt!, voice(tr({ status: 'failed' }))).button).toBe('Расшифровать заново');
    expect(transcriptView(voiceAtt!, voice(tr({ status: 'working' }))).button).toBeNull();
    expect(transcriptView(voiceAtt!, voice(null, { status: 'deferred' })).button).toBeNull();
    expect(transcriptView(videoAtt!, voice(null, { kind: 'video', detected: 'mp4' })).button).toBeNull();
    expect(transcriptView(roundAtt!, voice(null, { kind: 'video', detected: 'mp4' })).button).toBe(
      'Расшифровать',
    );
  });

  it('состояния: «пусто» — silence, problem — doubtful, en — пометка', () => {
    expect(transcriptView(voiceAtt!, voice(tr({ text: '', problem: 'пусто' }))).state).toBe('silence');
    const doubtful = transcriptView(voiceAtt!, voice(tr({ problem: 'зацикливание' })));
    expect(doubtful.state).toBe('done');
    expect(doubtful.doubtful).toBe(true);
    expect(transcriptView(voiceAtt!, voice(tr({ lang: 'en' }))).langMark).toBe('en');
    expect(transcriptView(voiceAtt!, voice(tr({ lang: 'ru' }))).langMark).toBeNull();
  });

  it('свёртка длинного текста и текст ошибки кнопки', () => {
    expect(foldTranscript('коротко').folded).toBe(false);
    const long = foldTranscript('а'.repeat(400));
    expect(long.folded).toBe(true);
    expect(long.short.length).toBe(301);
    expect(transcribeErrorText(409, 'transcribe_busy')).toBe('уже расшифровываю');
    expect(transcribeErrorText(409, 'media_not_ready')).toBe('сначала загрузи файл');
    expect(transcribeErrorText(422, 'not transcribable')).toBe('этот файл не расшифровать');
    expect(transcribeErrorText(null, null)).toBe('не получилось, попробуй ещё раз');
  });
});

describe('54 transcript: блок под плеером', () => {
  it('готовая расшифровка — простым текстом под <audio>, кнопка «Расшифровать заново»', () => {
    const wrapper = row(voice(tr({ text: '<b>жирно</b> и *звёздочки*' })));
    const block = wrapper.get('li .transcript');
    expect(wrapper.find('li audio.media-audio').exists()).toBe(true);
    const text = block.get('.transcript__text');
    expect(text.text()).toBe('<b>жирно</b> и *звёздочки*');
    expect(text.find('b').exists()).toBe(false);
    expect(block.get('button.transcript__action').text()).toBe('Расшифровать заново');
    expect(block.find('.transcript__lang').exists()).toBe(false);
  });

  it('en — пометка языка; problem — серая строка; «пусто» — «речи не слышно»', () => {
    const en = row(voice(tr({ lang: 'en', text: 'call me back', problem: 'мало слов: 3 на 20 с речи' })));
    expect(en.get('.transcript__lang').text()).toContain('en');
    expect(en.get('.transcript').text()).toContain('расшифровка может быть неточной');
    const silent = row(voice(tr({ text: '', problem: 'пусто', lang: null })));
    expect(silent.get('.transcript').text()).toContain('речи не слышно');
    expect(silent.find('.transcript__text').exists()).toBe(false);
    expect(silent.get('.transcript').text()).not.toContain('неточной');
  });

  it('queued/working — «расшифровываю…» без кнопки; failed — «не удалось расшифровать»', () => {
    const queued = row(voice(tr({ status: 'queued', text: null })));
    expect(queued.get('.transcript').text()).toBe('расшифровываю…');
    expect(queued.find('button.transcript__action').exists()).toBe(false);
    const failed = row(voice(tr({ status: 'failed', text: null })));
    expect(failed.get('.transcript').text()).toContain('не удалось расшифровать');
    expect(failed.get('button.transcript__action').text()).toBe('Расшифровать заново');
  });

  it('длинный текст свёрнут, «показать целиком» раскрывает', async () => {
    const long = 'слово '.repeat(100).trim();
    const wrapper = row(voice(tr({ text: long })));
    expect(wrapper.get('.transcript__text').text().length).toBeLessThan(long.length);
    const more = wrapper.get('button.transcript__more');
    expect(more.text()).toBe('показать целиком');
    await more.trigger('click');
    // Ф99-6: полный текст — окном поверх, лента остаётся свёрнутой.
    expect(document.body.querySelector('[data-test="transcript-modal-text"]')?.textContent?.trim()).toBe(long);
    expect(wrapper.get('.transcript__text').text().length).toBeLessThan(long.length);
    wrapper.unmount();
  });

  it('нет строки — «Расшифровать» зовёт transcribeMedia(media_id); обычное видео — без блока', async () => {
    const spy = vi.spyOn(store, 'transcribeMedia').mockResolvedValue();
    const wrapper = row(voice(null, { id: 77 }));
    const button = wrapper.get('button.transcript__action');
    expect(button.text()).toBe('Расшифровать');
    await button.trigger('click');
    expect(spy).toHaveBeenCalledWith(77);

    const video = row(voice(null, { kind: 'video', detected: 'mp4' }), [{ type: 'video' }]);
    expect(video.find('video.media-video').exists()).toBe(true);
    expect(video.find('.transcript').exists()).toBe(false);
    const round = row(voice(tr({}), { kind: 'video', detected: 'mp4' }), [{ type: 'video', round: true }]);
    expect(round.get('.transcript__text').text()).toBe('привет, перезвони');
  });

  it('ошибка кнопки — текстом рядом', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(store, 'transcribeMedia').mockRejectedValue(new api.ApiError(409, 'transcribe_busy', null));
    const wrapper = row(voice(tr({})));
    await wrapper.get('button.transcript__action').trigger('click');
    await new Promise((r) => setTimeout(r, 0));
    expect(wrapper.get('.transcript .media-status--error').text()).toBe('уже расшифровываю');
  });
});

describe('54 transcript: store.transcribeMedia', () => {
  it('POST /media/{id}/transcribe и обновление media сообщения', async () => {
    store.state.sync.messagesByChannel[1] = [msg({ id: 42, media: [voice(null)] })];
    vi.spyOn(api, 'transcribeMedia').mockResolvedValue({
      message_id: 42,
      media: [voice(tr({ status: 'queued', text: null, manual: true }))],
    });
    vi.spyOn(store, 'runSync').mockResolvedValue();
    await store.transcribeMedia(5);
    expect(api.transcribeMedia).toHaveBeenCalledWith(5);
    expect(store.state.sync.messagesByChannel[1]![0]!.media![0]!.transcript!.status).toBe('queued');
    delete store.state.sync.messagesByChannel[1];
  });
});
