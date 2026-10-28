// Ф8-1 C (`docs/architecture.md`), тест T8: удалённое отправителем остаётся видно — приглушённо,
// с пометкой «удалено», без «Ответить». Удалённое без текста (старое ядро) — «сообщение удалено», как раньше.
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import AlbumRow from '../src/components/AlbumRow.vue';
import MessageRow from '../src/components/MessageRow.vue';
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
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
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
    sender: { id: 1, name: 'Коллега', handle: null },
    ...overrides,
  };
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
});

describe('T8: MessageRow — удалённое', () => {
  it('удалённое с текстом: текст виден, пометка «удалено», приглушено, «Ответить» нет', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          deleted: true,
          text: 'текст, который удалил отправитель',
          attachments_json: JSON.stringify([{ type: 'photo', name: 'снимок.jpg' }]),
          forward_json: JSON.stringify({ from_name: 'Иван' }),
          reply_to_ext_id: '0',
        }),
      },
    });
    expect(wrapper.text()).toContain('текст, который удалил отправитель');
    expect(wrapper.find('.deleted-mark').text()).toBe('удалено');
    expect(wrapper.find('li').classes()).toContain('message-row--deleted');
    expect(wrapper.find('.reply-button').exists()).toBe(false);
    expect(wrapper.find('.message-deleted').exists()).toBe(false);
    expect(wrapper.find('.attachment').exists()).toBe(true);
    expect(wrapper.find('.forwarded').exists()).toBe(true);
    expect(wrapper.find('.reply-quote').exists()).toBe(true);
  });

  it('удалённое с text: null и без вложений/пересылки — «сообщение удалено», как раньше', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({ deleted: true, text: null }) } });
    expect(wrapper.find('.message-deleted').text()).toBe('сообщение удалено');
    expect(wrapper.find('.reply-button').exists()).toBe(false);
  });

  it('удалённое фото без подписи — вложение видно, пометка «удалено», приглушено', () => {
    const wrapper = mount(MessageRow, {
      props: {
        message: msg({
          deleted: true,
          text: null,
          attachments_json: JSON.stringify([{ type: 'photo', name: 'снимок.jpg' }]),
        }),
      },
    });
    expect(wrapper.find('.attachment').exists()).toBe(true);
    expect(wrapper.find('.deleted-mark').text()).toBe('удалено');
    expect(wrapper.find('li').classes()).toContain('message-row--deleted');
    expect(wrapper.find('.message-deleted').exists()).toBe(false);
    expect(wrapper.find('.reply-button').exists()).toBe(false);
  });

  it('удалённая пересылка без текста — «переслано от» видно, без «сообщение удалено»', () => {
    const wrapper = mount(MessageRow, {
      props: { message: msg({ deleted: true, text: null, forward_json: JSON.stringify({ from_name: 'Иван' }) }) },
    });
    expect(wrapper.find('.forwarded').text()).toContain('Иван');
    expect(wrapper.find('.message-deleted').exists()).toBe(false);
  });

  it('обычное сообщение — без пометки и без приглушения, «Ответить» есть', () => {
    const wrapper = mount(MessageRow, { props: { message: msg({}) } });
    expect(wrapper.find('.deleted-mark').exists()).toBe(false);
    expect(wrapper.find('li').classes()).not.toContain('message-row--deleted');
    expect(wrapper.find('.reply-button').exists()).toBe(true);
  });
});

describe('Ф8-1 C2: AlbumRow — удалённое остаётся видно', () => {
  it('альбом удалён целиком, подпись есть — подпись и вложения видны, пометка «удалено»', () => {
    const wrapper = mount(AlbumRow, {
      props: {
        messages: [
          msg({ id: 1, grouped_id: 'g', deleted: true, text: 'подпись альбома', attachments_json: JSON.stringify([{ type: 'photo' }]) }),
          msg({ id: 2, grouped_id: 'g', deleted: true, text: '', attachments_json: JSON.stringify([{ type: 'photo' }]) }),
        ],
      },
    });
    expect(wrapper.text()).toContain('подпись альбома');
    expect(wrapper.findAll('.attachment')).toHaveLength(2);
    expect(wrapper.find('.deleted-mark').text()).toBe('удалено');
    expect(wrapper.find('li').classes()).toContain('message-row--deleted');
    expect(wrapper.find('.message-deleted').exists()).toBe(false);
  });

  it('альбом фото без подписей удалён целиком — вложения видны, «сообщение удалено» нет', () => {
    const wrapper = mount(AlbumRow, {
      props: {
        messages: [
          msg({ id: 1, grouped_id: 'g', deleted: true, text: null, attachments_json: JSON.stringify([{ type: 'photo' }]) }),
          msg({ id: 2, grouped_id: 'g', deleted: true, text: null, attachments_json: JSON.stringify([{ type: 'photo' }]) }),
        ],
      },
    });
    expect(wrapper.findAll('.attachment')).toHaveLength(2);
    expect(wrapper.find('.deleted-mark').text()).toBe('удалено');
    expect(wrapper.find('.message-deleted').exists()).toBe(false);
  });

  it('альбом удалён целиком, без подписей и вложений — «сообщение удалено»', () => {
    const wrapper = mount(AlbumRow, {
      props: {
        messages: [
          msg({ id: 1, grouped_id: 'g', deleted: true, text: null }),
          msg({ id: 2, grouped_id: 'g', deleted: true, text: null }),
        ],
      },
    });
    expect(wrapper.find('.message-deleted').text()).toBe('сообщение удалено');
  });

  it('удалено одно из двух — пометка «удалено 1 из 2», строка не приглушена', () => {
    const wrapper = mount(AlbumRow, {
      props: {
        messages: [
          msg({ id: 1, grouped_id: 'g', deleted: true, text: 'удалённая подпись' }),
          msg({ id: 2, grouped_id: 'g', text: 'живая подпись' }),
        ],
      },
    });
    expect(wrapper.text()).toContain('живая подпись');
    expect(wrapper.find('.deleted-mark').text()).toBe('удалено 1 из 2');
    expect(wrapper.find('li').classes()).not.toContain('message-row--deleted');
  });
});
