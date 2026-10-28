// Ф14-8 (`docs/architecture.md`, тесты E13–E15, поправки П1, П2, П6): «Изменить» и
// меню «Удалить» (у себя / у всех) у своих сообщений Telegram/Slack, правка на месте, состояние операции `op`,
// сводка операций альбома; стор кладёт `op` из ответа ручки сразу.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, Message, MessageOp } from '../src/api/types';
import AlbumRow from '../src/components/AlbumRow.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { createStore } from '../src/store/store';
import { store } from '../src/store/instance';
import { ENTITIES_LOST_WARNING, REVOKE_CONFIRM, canEdit, editLosesEntities } from '../src/util/msgOps';

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
    is_me: true,
    kind: 'text',
    text: 'привет',
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
    sender: { id: 1, name: 'Я', handle: null },
    ...overrides,
  };
}

function channel(source: string | undefined, extId = 'c5'): Channel {
  return {
    id: 5,
    ext_id: extId,
    kind: 'dm',
    title: 'Беседа',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 0,
    unread: 0,
    project_id: 1,
    source,
  };
}

function withSource(source: string | undefined, extId = 'c5'): void {
  store.state.sync.channelsById = { 5: channel(source, extId) };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

const editQueued = (text: string): MessageOp => ({ kind: 'edit', state: 'queued', text, error: null });
const deleteQueued: MessageOp = { kind: 'delete', state: 'queued', error: null };
function failed(kind: 'edit' | 'delete', code: string, detail: string | null = null, text = 'новое'): MessageOp {
  const op: MessageOp = { kind, state: 'failed', error: { code, detail } };
  if (kind === 'edit') op.text = text;
  return op;
}

function buttonTexts(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('.message-meta button').map((b) => b.text());
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  store.state.sync.threads = {};
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('E13: кнопки «Изменить» и меню «Удалить»', () => {
  it('своё Telegram/Slack с ext_id — «Изменить» и меню; у чужого и у почты — одна «Удалить у себя»', () => {
    for (const source of ['telegram', 'slack']) {
      withSource(source);
      const own = mount(MessageRow, { props: { message: msg({}) } });
      expect(buttonTexts(own)).toContain('Изменить');
      expect(own.find('button.delete-menu-button').exists()).toBe(true);
      expect(own.get('button.hide-button').text()).toBe('Удалить ▾');

      const other = mount(MessageRow, { props: { message: msg({ is_me: false }) } });
      expect(buttonTexts(other)).not.toContain('Изменить');
      expect(other.find('button.delete-menu-button').exists()).toBe(false);
      expect(other.get('button.hide-button').text()).toBe('Удалить у себя');
    }
    withSource('mail');
    const mail = mount(MessageRow, { props: { message: msg({}) } });
    expect(buttonTexts(mail)).not.toContain('Изменить');
    expect(mail.find('button.delete-menu-button').exists()).toBe(false);
    expect(mail.get('button.hide-button').text()).toBe('Удалить у себя');
  });

  it('без ext_id, удалённое, беседа-заготовка — ни «Изменить», ни «у всех»', () => {
    withSource('telegram');
    for (const message of [msg({ ext_id: null }), msg({ deleted: true })]) {
      const w = mount(MessageRow, { props: { message } });
      expect(buttonTexts(w)).not.toContain('Изменить');
      expect(w.find('button.delete-menu-button').exists()).toBe(false);
    }
    withSource('telegram', 'new:abc');
    const temp = mount(MessageRow, { props: { message: msg({}) } });
    expect(temp.find('button.delete-menu-button').exists()).toBe(false);
  });

  it('пересланное — без «Изменить», но «у всех» есть; ждущее удаление — без «Изменить»', () => {
    withSource('telegram');
    const fwd = mount(MessageRow, { props: { message: msg({ forward_json: '{"from":"Кто-то"}' }) } });
    expect(buttonTexts(fwd)).not.toContain('Изменить');
    expect(fwd.find('button.delete-menu-button').exists()).toBe(true);
    const pending = mount(MessageRow, { props: { message: msg({ op: deleteQueued }) } });
    expect(buttonTexts(pending)).not.toContain('Изменить');
  });

  it('П6: файл Telegram — только если все вложения правятся; Slack — любой файл', () => {
    const tg = channel('telegram');
    const file = (attachments: unknown) => msg({ kind: 'file', attachments_json: JSON.stringify(attachments) });
    expect(canEdit(file([{ type: 'photo' }, { type: 'document' }]), tg)).toBe(true);
    expect(canEdit(file([{ type: 'voice' }]), tg)).toBe(true);
    expect(canEdit(file([{ type: 'video' }]), tg)).toBe(true);
    expect(canEdit(file([{ type: 'video', round: true }]), tg)).toBe(false);
    expect(canEdit(file([{ type: 'photo' }, { type: 'sticker' }]), tg)).toBe(false);
    expect(canEdit(file([{ type: 'poll' }]), tg)).toBe(false);
    expect(canEdit(file([]), tg)).toBe(false);
    expect(canEdit(file([{ type: 'sticker' }]), channel('slack'))).toBe(true);
    expect(canEdit(msg({ kind: 'event' }), tg)).toBe(false);
  });

  it('меню: «у себя» — прежний путь с подтверждением; «у всех» — своё подтверждение, отказ — запроса нет', async () => {
    withSource('telegram');
    const hide = vi.spyOn(store, 'hideMessages').mockResolvedValue();
    const revoke = vi.spyOn(store, 'revokeMessages').mockResolvedValue();
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal('confirm', confirm);
    const w = mount(MessageRow, { props: { message: msg({ id: 42 }) } });

    await w.get('button.delete-menu-button').trigger('click');
    await w.get('button.revoke-item').trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenLastCalledWith(REVOKE_CONFIRM);
    expect(revoke).not.toHaveBeenCalled();
    expect(w.find('.delete-menu').exists()).toBe(false);

    confirm.mockReturnValue(true);
    await w.get('button.delete-menu-button').trigger('click');
    await w.get('button.revoke-item').trigger('click');
    await flushPromises();
    expect(revoke).toHaveBeenCalledWith([42]);

    await w.get('button.delete-menu-button').trigger('click');
    expect(w.get('button.hide-self-item').text()).toBe('Удалить у себя (только в хабе)');
    await w.get('button.hide-self-item').trigger('click');
    await flushPromises();
    expect(hide).toHaveBeenCalledWith([42]);
  });

  it('П1: пока есть op — «Удалить у себя» недоступна', async () => {
    withSource('telegram');
    const hide = vi.spyOn(store, 'hideMessages').mockResolvedValue();
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    const w = mount(MessageRow, { props: { message: msg({ op: failed('delete', 'forbidden') }) } });
    await w.get('button.delete-menu-button').trigger('click');
    const item = w.get('button.hide-self-item');
    expect(item.attributes('disabled')).toBeDefined();
    await item.trigger('click');
    await flushPromises();
    expect(hide).not.toHaveBeenCalled();
  });

  it('Esc закрывает меню', async () => {
    withSource('slack');
    const w = mount(MessageRow, { props: { message: msg({}) }, attachTo: document.body });
    await w.get('button.delete-menu-button').trigger('click');
    expect(w.find('.delete-menu').exists()).toBe(true);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(w.find('.delete-menu').exists()).toBe(false);
    w.unmount();
  });
});

describe('E14: правка на месте', () => {
  it('поле с текстом; «Сохранить» недоступна без изменений и с пустым; Ctrl+Enter сохраняет', async () => {
    withSource('telegram');
    const edit = vi.spyOn(store, 'editMessage').mockResolvedValue('queued');
    const w = mount(MessageRow, { props: { message: msg({ id: 7, text: 'старый' }) } });
    await w.findAll('button').find((b) => b.text() === 'Изменить')!.trigger('click');
    const field = w.get('textarea.message-edit__field');
    expect((field.element as HTMLTextAreaElement).value).toBe('старый');
    const save = w.get('button.message-edit__save');
    expect(save.attributes('disabled')).toBeDefined();

    await field.setValue('   ');
    expect(save.attributes('disabled')).toBeDefined();
    await field.setValue('новый');
    expect(save.attributes('disabled')).toBeUndefined();

    await field.trigger('keydown', { key: 'Enter', ctrlKey: true });
    await flushPromises();
    expect(edit).toHaveBeenCalledWith(7, 'новый');
    expect(w.find('textarea').exists()).toBe(false);
  });

  it('Esc и «Отмена» закрывают поле без запроса', async () => {
    withSource('slack');
    const edit = vi.spyOn(store, 'editMessage').mockResolvedValue('queued');
    const w = mount(MessageRow, { props: { message: msg({}) } });
    const open = () => w.findAll('button').find((b) => b.text() === 'Изменить')!.trigger('click');
    await open();
    await w.get('textarea').trigger('keydown', { key: 'Escape' });
    expect(w.find('textarea').exists()).toBe(false);
    await open();
    await w.get('button.message-edit__cancel').trigger('click');
    expect(w.find('textarea').exists()).toBe(false);
    expect(edit).not.toHaveBeenCalled();
    expect(w.text()).toContain('привет');
  });

  it('422 — ошибка под полем, поле открыто', async () => {
    withSource('telegram');
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(store, 'editMessage').mockRejectedValue(new api.ApiError(422, 'too_long: не больше 4096 символов', null));
    const w = mount(MessageRow, { props: { message: msg({}) } });
    await w.findAll('button').find((b) => b.text() === 'Изменить')!.trigger('click');
    await w.get('textarea').setValue('длинно');
    await w.get('button.message-edit__save').trigger('click');
    await flushPromises();
    expect(w.find('textarea').exists()).toBe(true);
    expect(w.get('.message-edit__error').text()).toContain('текст слишком длинный');
  });

  it('неудавшаяся правка — поле заполнено её текстом', async () => {
    withSource('telegram');
    const w = mount(MessageRow, { props: { message: msg({ op: failed('edit', 'expired', null, 'моя правка') }) } });
    await w.findAll('button').find((b) => b.text() === 'Изменить')!.trigger('click');
    expect((w.get('textarea').element as HTMLTextAreaElement).value).toBe('моя правка');
  });

  it('предупреждение про entities — по правилам раздела D', async () => {
    const text = '@user https://a.ru жирный';
    const mention = { type: 'mention', offset: 0, length: 5 };
    const bareLink = { type: 'link', offset: 6, length: 12, url: 'https://a.ru' };
    const bold = { type: 'bold', offset: 19, length: 6 };
    const json = (items: unknown[]) => JSON.stringify(items);
    expect(editLosesEntities('telegram', text, json([mention, bareLink]))).toBe(false);
    expect(editLosesEntities('telegram', text, json([mention, bareLink, bold]))).toBe(true);
    // ссылка под текстом и упоминание по имени (без «@») — пропадут
    expect(editLosesEntities('telegram', text, json([{ ...bareLink, url: 'https://b.ru' }]))).toBe(true);
    expect(editLosesEntities('telegram', 'Иван', json([{ type: 'mention', offset: 0, length: 4 }]))).toBe(true);
    expect(editLosesEntities('slack', text, json([bareLink]))).toBe(false);
    expect(editLosesEntities('slack', text, json([mention]))).toBe(true);
    // незнакомый тип (не из формата рендера) — тоже пропадёт
    expect(editLosesEntities('telegram', text, json([{ type: 'spoiler', offset: 0, length: 1 }]))).toBe(true);
    expect(editLosesEntities('telegram', text, null)).toBe(false);

    withSource('telegram');
    const w = mount(MessageRow, { props: { message: msg({ text, entities_json: json([bold]) }) } });
    await w.findAll('button').find((b) => b.text() === 'Изменить')!.trigger('click');
    expect(w.get('.message-edit__warning').text()).toBe(ENTITIES_LOST_WARNING);
    const plain = mount(MessageRow, { props: { message: msg({ text, entities_json: json([mention]) }) } });
    await plain.findAll('button').find((b) => b.text() === 'Изменить')!.trigger('click');
    expect(plain.find('.message-edit__warning').exists()).toBe(false);
  });
});

describe('E15: состояние операции', () => {
  it('queued edit — показан op.text и «сохраняется…»; «Отменить» — DELETE …/op, 409 in_flight — «уже отправляется»', async () => {
    withSource('telegram');
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const cancel = vi
      .spyOn(store, 'cancelMessageOps')
      .mockRejectedValue(new api.ApiError(409, 'in_flight', null));
    const w = mount(MessageRow, { props: { message: msg({ id: 9, text: 'старое', op: editQueued('новое') }) } });
    expect(w.text()).toContain('новое');
    expect(w.text()).not.toContain('старое');
    expect(w.get('.msg-op-line').text()).toContain('сохраняется…');
    await w.get('button.msg-op-cancel').trigger('click');
    await flushPromises();
    expect(cancel).toHaveBeenCalledWith([9]);
    expect(w.get('.msg-op-error').text()).toBe('уже отправляется');
  });

  it('queued delete — «удаляется у всех…» и «Отменить»', async () => {
    withSource('slack');
    const cancel = vi.spyOn(store, 'cancelMessageOps').mockResolvedValue();
    const w = mount(MessageRow, { props: { message: msg({ id: 9, op: deleteQueued }) } });
    expect(w.get('.msg-op-line').text()).toContain('удаляется у всех…');
    await w.get('button.msg-op-cancel').trigger('click');
    expect(cancel).toHaveBeenCalledWith([9]);
  });

  it('failed — причина по коду; «Повторить» шлёт тот же запрос, «Скрыть» — DELETE …/op', async () => {
    withSource('telegram');
    const edit = vi.spyOn(store, 'editMessage').mockResolvedValue('queued');
    const revoke = vi.spyOn(store, 'revokeMessages').mockResolvedValue();
    const cancel = vi.spyOn(store, 'cancelMessageOps').mockResolvedValue();
    const cases: [MessageOp, string][] = [
      [failed('edit', 'expired'), 'Не изменено: истёк срок правки у источника (Telegram — 48 часов)'],
      [failed('edit', 'too_long'), 'Не изменено: текст длиннее, чем разрешает источник'],
      [failed('delete', 'forbidden'), 'Не удалено у всех: источник не разрешает'],
      [failed('delete', 'not_found'), 'Не удалено у всех: сообщения больше нет у источника'],
      [failed('edit', 'other', 'not_modified'), 'Не изменено: не получилось (not_modified)'],
    ];
    for (const [op, label] of cases) {
      const w = mount(MessageRow, { props: { message: msg({ op }) } });
      expect(w.get('.msg-op-line--failed .msg-op-label').text()).toBe(label);
    }

    const we = mount(MessageRow, { props: { message: msg({ id: 3, op: failed('edit', 'expired', null, 'правка') }) } });
    await we.get('button.msg-op-retry').trigger('click');
    expect(edit).toHaveBeenCalledWith(3, 'правка');
    await we.get('button.msg-op-dismiss').trigger('click');
    expect(cancel).toHaveBeenCalledWith([3]);

    const wd = mount(MessageRow, { props: { message: msg({ id: 4, op: failed('delete', 'forbidden') }) } });
    await wd.get('button.msg-op-retry').trigger('click');
    expect(revoke).toHaveBeenCalledWith([4]);
  });

  it('стор: ответ ручки сразу кладёт op в ленту и тред, отмена — снимает', async () => {
    const s = createStore();
    s.state.sync.messagesByChannel[5] = [msg({ id: 7 }), msg({ id: 8, ext_id: '11' })];
    s.state.sync.threads['5:10'] = { parent: msg({ id: 7 }), replies: [], nextBefore: null, loading: false };
    const calls: string[] = [];
    const op = editQueued('новое');
    const fetchMock = vi.fn((url: string, init: { method: string; body?: string }) => {
      calls.push(`${init.method} ${url} ${init.body ?? ''}`);
      if (url.startsWith('/api/v1/sync')) {
        return Promise.resolve(jsonResponse({ messages: [], channels: [], max_seq: 0, more: false }));
      }
      if (url.startsWith('/api/v1/status')) return Promise.resolve(jsonResponse({ max_seq: 0 }));
      if (url === '/api/v1/messages/7/edit') return Promise.resolve(jsonResponse({ status: 'queued', op }));
      if (url === '/api/v1/messages/revoke') {
        return Promise.resolve(jsonResponse({ status: 'queued', ops: { '8': deleteQueued } }));
      }
      return Promise.resolve(new Response(null, { status: 204 }));
    });
    vi.stubGlobal('fetch', fetchMock);

    expect(await s.editMessage(7, 'новое')).toBe('queued');
    expect(calls[0]).toBe('POST /api/v1/messages/7/edit {"text":"новое"}');
    expect(s.state.sync.messagesByChannel[5]![0]!.op).toEqual(op);
    expect(s.state.sync.threads['5:10']!.parent!.op).toEqual(op);

    await s.revokeMessages([8]);
    expect(calls).toContain('POST /api/v1/messages/revoke {"message_ids":[8]}');
    expect(s.state.sync.messagesByChannel[5]![1]!.op).toEqual(deleteQueued);

    await s.cancelMessageOps([7]);
    expect(calls).toContain('DELETE /api/v1/messages/7/op ');
    expect(s.state.sync.messagesByChannel[5]![0]!.op).toBeNull();
  });
});

describe('П2: альбом', () => {
  const photo = JSON.stringify([{ type: 'photo' }]);
  const part = (id: number, over: Partial<Message> = {}) =>
    msg({ id, ext_id: String(100 + id), kind: 'file', text: null, grouped_id: 'g', attachments_json: photo, ...over });

  it('«у всех» шлёт id неудалённых частей одним запросом; «Изменить» нет', async () => {
    withSource('telegram');
    const revoke = vi.spyOn(store, 'revokeMessages').mockResolvedValue();
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
    const w = mount(AlbumRow, { props: { messages: [part(1), part(2, { deleted: true }), part(3)] } });
    expect(w.findAll('button').some((b) => b.text() === 'Изменить')).toBe(false);
    await w.get('button.delete-menu-button').trigger('click');
    await w.get('button.revoke-item').trigger('click');
    await flushPromises();
    expect(revoke).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith([1, 3]);
  });

  it('чужой альбом — одна «Удалить у себя»', () => {
    withSource('telegram');
    const w = mount(AlbumRow, { props: { messages: [part(1, { is_me: false }), part(2, { is_me: false })] } });
    expect(w.find('button.delete-menu-button').exists()).toBe(false);
    expect(w.get('button.hide-button').text()).toBe('Удалить у себя');
  });

  it('сводка: queued — «удаляется у всех…», «Отменить» снимает строки частей', async () => {
    withSource('telegram');
    const cancel = vi.spyOn(store, 'cancelMessageOps').mockResolvedValue();
    const w = mount(AlbumRow, { props: { messages: [part(1, { op: deleteQueued }), part(2, { op: deleteQueued })] } });
    expect(w.get('.msg-op-line--queued').text()).toContain('удаляется у всех…');
    await w.get('button.msg-op-cancel').trigger('click');
    expect(cancel).toHaveBeenCalledWith([1, 2]);
    await w.get('button.delete-menu-button').trigger('click');
    expect(w.get('button.hide-self-item').attributes('disabled')).toBeDefined();
  });

  it('сводка: failed — «Не удалено у всех (N из M)», «Повторить» и «Скрыть» — только failed-части', async () => {
    withSource('telegram');
    const revoke = vi.spyOn(store, 'revokeMessages').mockResolvedValue();
    const cancel = vi.spyOn(store, 'cancelMessageOps').mockResolvedValue();
    const w = mount(AlbumRow, {
      props: { messages: [part(1), part(2, { op: failed('delete', 'forbidden') }), part(3)] },
    });
    expect(w.get('.msg-op-line--failed .msg-op-label').text()).toBe(
      'Не удалено у всех (1 из 3): источник не разрешает',
    );
    await w.get('button.msg-op-retry').trigger('click');
    expect(revoke).toHaveBeenCalledWith([2]);
    await w.get('button.msg-op-dismiss').trigger('click');
    expect(cancel).toHaveBeenCalledWith([2]);
  });
});
