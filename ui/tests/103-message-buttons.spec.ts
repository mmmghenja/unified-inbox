// Ф44-1b: инлайн-кнопки бота Telegram под сообщением (`docs/architecture.md`).
import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Message, MessageButtons as Buttons } from '../src/api/types';
import MessageButtons from '../src/components/MessageButtons.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { applySync, createSyncState } from '../src/store/applySync';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 1,
    ext_id: '10',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'меню бота',
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
    sender: { id: 1, name: 'Бот', handle: null },
    ...overrides,
  };
}

const KB: Buttons = {
  kind: 'inline',
  rows: [
    [
      { t: 'Продлить', k: 'callback' },
      { t: 'Тариф (нужен пароль)', k: 'callback', pw: true },
    ],
    [{ t: 'Сайт', k: 'url', u: 'https://example.com/path?x=1' }],
    [
      { t: 'Скрипт', k: 'url', u: 'javascript:alert(1)' },
      { t: 'Промокод', k: 'copy', c: 'PROMO-42' },
      { t: 'Оплата', k: 'other' },
    ],
  ],
};

afterEach(() => vi.restoreAllMocks());

describe('44-1b MessageButtons', () => {
  it('рисует ряды: число рядов и кнопок как в данных', () => {
    const w = mount(MessageButtons, { props: { buttons: KB } });
    const rows = w.findAll('.msg-buttons__row');
    expect(rows.map((r) => r.findAll('.msg-btn').length)).toEqual([2, 1, 3]);
  });

  it('callback — активная кнопка, клик даёт press с адресом (с 1) и текстом', async () => {
    const w = mount(MessageButtons, { props: { buttons: KB } });
    const first = w.findAll('.msg-btn')[0]!;
    expect(first.attributes('disabled')).toBeUndefined();
    await first.trigger('click');
    expect(w.emitted('press')).toEqual([[{ row: 1, col: 1, text: 'Продлить' }]]);
  });

  it('callback с pw — неактивна с подсказкой, press не шлёт', async () => {
    const w = mount(MessageButtons, { props: { buttons: KB } });
    const pw = w.findAll('.msg-btn')[1]!;
    expect(pw.attributes('disabled')).toBeDefined();
    expect(pw.attributes('title')).toBe('требует пароль Telegram');
    await pw.trigger('click');
    expect(w.emitted('press')).toBeUndefined();
  });

  it('url — ссылка с rel, target, доменом под текстом и полным адресом в подсказке', () => {
    const w = mount(MessageButtons, { props: { buttons: KB } });
    const link = w.find('a.msg-btn');
    expect(link.attributes('href')).toBe('https://example.com/path?x=1');
    expect(link.attributes('target')).toBe('_blank');
    expect(link.attributes('rel')).toBe('noopener noreferrer');
    expect(link.attributes('title')).toBe('https://example.com/path?x=1');
    expect(link.find('.msg-btn__host').text()).toBe('example.com');
  });

  it('url не http/https — неактивная кнопка без ссылки', () => {
    const w = mount(MessageButtons, { props: { buttons: KB } });
    expect(w.findAll('a')).toHaveLength(1);
    const bad = w.findAll('.msg-btn').find((b) => b.text().includes('Скрипт'))!;
    expect(bad.element.tagName).toBe('BUTTON');
    expect(bad.attributes('disabled')).toBeDefined();
    expect(bad.html()).not.toContain('javascript:');
  });

  it('other — неактивна, подсказка «только в приложении Telegram»', () => {
    const w = mount(MessageButtons, { props: { buttons: KB } });
    const other = w.findAll('.msg-btn').find((b) => b.text().includes('Оплата'))!;
    expect(other.attributes('disabled')).toBeDefined();
    expect(other.attributes('title')).toBe('только в приложении Telegram');
  });

  it('copy — копирует текст и показывает «скопировано»', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    const w = mount(MessageButtons, { props: { buttons: KB } });
    const copy = w.find('.msg-btn--copy');
    expect(copy.find('.msg-btn__host').text()).toBe('скопировать');
    await copy.trigger('click');
    await vi.waitFor(() => expect(copy.find('.msg-btn__host').text()).toBe('скопировано'));
    expect(writeText).toHaveBeenCalledWith('PROMO-42');
    vi.unstubAllGlobals();
  });

  it('cut — строка «часть кнопок не показана»; без cut её нет', () => {
    const cut = mount(MessageButtons, { props: { buttons: { ...KB, cut: true } } });
    expect(cut.text()).toContain('часть кнопок не показана');
    const whole = mount(MessageButtons, { props: { buttons: KB } });
    expect(whole.text()).not.toContain('часть кнопок не показана');
  });

  it('текст кнопки — только текстом, разметка не исполняется', () => {
    const w = mount(MessageButtons, {
      props: { buttons: { kind: 'inline', rows: [[{ t: '<b>x</b><img src=x>', k: 'callback' }]] } },
    });
    expect(w.find('b').exists()).toBe(false);
    expect(w.find('img').exists()).toBe(false);
    expect(w.text()).toBe('<b>x</b><img src=x>');
  });
});

describe('44-1b MessageRow и sync', () => {
  it('кнопки видны в строке сообщения; без кнопок и с пустыми рядами — нет', () => {
    const withButtons = mount(MessageRow, { props: { message: msg({ buttons: KB }) } });
    expect(withButtons.find('[data-testid="msg-buttons"]').exists()).toBe(true);
    const none = mount(MessageRow, { props: { message: msg({ buttons: null }) } });
    expect(none.find('[data-testid="msg-buttons"]').exists()).toBe(false);
    const empty = mount(MessageRow, { props: { message: msg({ buttons: { kind: 'inline', rows: [] } }) } });
    expect(empty.find('[data-testid="msg-buttons"]').exists()).toBe(false);
  });

  it('applySync: правка меняет кнопки, снятие клавиатуры даёт null', () => {
    const state = createSyncState();
    applySync(state, { messages: [msg({ buttons: KB })], channels: [], max_seq: 1 });
    expect(state.messagesByChannel[5]![0]!.buttons?.rows).toHaveLength(3);

    const next: Buttons = { kind: 'inline', rows: [[{ t: 'Готово', k: 'callback' }]] };
    applySync(state, { messages: [msg({ seq: 2, buttons: next })], channels: [], max_seq: 2 });
    expect(state.messagesByChannel[5]![0]!.buttons).toEqual(next);

    applySync(state, { messages: [msg({ seq: 3, buttons: null })], channels: [], max_seq: 3 });
    expect(state.messagesByChannel[5]![0]!.buttons).toBeNull();
  });
});
