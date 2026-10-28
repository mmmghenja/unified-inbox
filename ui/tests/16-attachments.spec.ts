// Спека 1d-1, п.1: вложения. Разбор `attachments_json` (`src/render/attachments.ts`) + показ в `MessageRow`.
import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import { formatAttachmentLabel, parseAttachments } from '../src/render/attachments';
import MessageRow from '../src/components/MessageRow.vue';

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
    attachments_json: null,
    forward_json: null,
    mentions_me: false,
    refs_json: null,
    reactions_json: null,
    ts: 1000,
    edited_ts: null,
    deleted: false,
    outbox_id: null,
    outbox_status: null,
    sender: null,
    ...overrides,
  };
}

describe('16 attachments: разбор и русские подписи по типу', () => {
  it('каждый известный тип получает свою русскую подпись', () => {
    const types: Array<[string, string]> = [
      ['photo', 'Фото'],
      ['video', 'Видео'],
      ['voice', 'Голосовое'],
      ['audio', 'Аудио'],
      ['document', 'Документ'],
      ['sticker', 'Стикер'],
      ['gif', 'GIF'],
      ['location', 'Геопозиция'],
      ['contact', 'Контакт'],
      ['poll', 'Опрос'],
      ['other', 'Файл'],
    ];
    for (const [type, label] of types) {
      const [att] = parseAttachments(JSON.stringify([{ type }]));
      expect(att).toBeDefined();
      expect(formatAttachmentLabel(att!)).toBe(label);
    }
  });

  it('неизвестный type отбрасывается, валидные соседи остаются', () => {
    const json = JSON.stringify([{ type: 'photo', name: 'a.jpg' }, { type: 'bogus' }, { type: 'video' }]);
    const parsed = parseAttachments(json);
    expect(parsed).toHaveLength(2);
    expect(parsed[0]!.type).toBe('photo');
    expect(parsed[1]!.type).toBe('video');
  });

  it('битый JSON / не-список / null -> пустой список, без падения', () => {
    expect(parseAttachments('не json')).toEqual([]);
    expect(parseAttachments('{"type":"photo"}')).toEqual([]); // объект, не массив
    expect(parseAttachments(null)).toEqual([]);
  });

  it('размер — человеческий: Б/КБ/МБ', () => {
    expect(formatAttachmentLabel(parseAttachments(JSON.stringify([{ type: 'document', size: 500 }]))[0]!)).toBe(
      'Документ 500 Б',
    );
    expect(
      formatAttachmentLabel(parseAttachments(JSON.stringify([{ type: 'document', size: 2048 }]))[0]!),
    ).toBe('Документ 2 КБ');
    expect(
      formatAttachmentLabel(parseAttachments(JSON.stringify([{ type: 'document', size: 5 * 1024 * 1024 }]))[0]!),
    ).toBe('Документ 5 МБ');
  });

  it('имя и размер — необязательны, отсутствующие части просто опускаются', () => {
    const [att] = parseAttachments(JSON.stringify([{ type: 'sticker' }]));
    expect(formatAttachmentLabel(att!)).toBe('Стикер');
  });
});

describe('16 attachments: ссылок «открыть в Telegram» нет (Ф8-5 B5, T10)', () => {
  it('source_url во вложении не превращается в ссылку — ни t.me, ни что-либо ещё', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: JSON.stringify([
            { type: 'photo', name: 'фото.jpg', size: 1024, source_url: 'https://t.me/c/1/2' },
            { type: 'document', name: 'план.pdf', source_url: 'https://t.me/some_channel/42' },
          ]),
        }),
      },
    });
    expect(wrapper.find('a').exists()).toBe(false);
    expect(wrapper.html()).not.toContain('t.me');
    expect(wrapper.text()).not.toContain('открыть в Telegram');
    expect(wrapper.text()).toContain('📎 Фото фото.jpg 1 КБ');
    expect(wrapper.text()).toContain('📎 Документ план.pdf');
  });

  it('разбор сохраняет исходный idx вложения, отброшенные соседи его не сдвигают', () => {
    const parsed = parseAttachments('[{"type":"bogus"}, {"type":"photo"}, "мусор", {"type":"video"}]');
    expect(parsed.map((a) => a.idx)).toEqual([1, 3]);
    expect(Object.keys(parsed[0]!)).not.toContain('sourceUrl');
  });

  it('MessageRow: битые/неполные вложения игнорируются, валидные показываются, без падения', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          attachments_json: '[{"type":"photo","name":"ok.jpg"}, {"type":"нет такого"}, "мусор", 42]',
        }),
      },
    });
    expect(wrapper.findAll('.attachment')).toHaveLength(1);
    expect(wrapper.text()).toContain('ok.jpg');
  });

  // Ф8-1 C2 (решение архитектора 24.09): удалённое остаётся видно — вложения показываются и у удалённого,
  // «сообщение удалено» вместо содержимого — только когда показать нечего.
  it('удалённое сообщение без подписи — вложение видно, пометка «удалено», без «сообщение удалено»', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          deleted: true,
          text: null,
          attachments_json: JSON.stringify([{ type: 'photo' }]),
        }),
      },
    });
    expect(wrapper.find('.attachment').exists()).toBe(true);
    expect(wrapper.find('.deleted-mark').text()).toBe('удалено');
    expect(wrapper.text()).not.toContain('сообщение удалено');
  });
});
