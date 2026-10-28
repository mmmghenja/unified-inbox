// Ф62-1 E: WhatsApp в интерфейсе — значок «WA» и слово в шапке, «✓✓» по времени сообщения (id не число), реакции,
// правка/удаление и файлы — по спискам источников, «Кому» по номеру предлагает и аккаунты WhatsApp.
import { describe, expect, it } from 'vitest';

import type { Account, Channel, Message } from '../src/api/types';
import { isReactionSource, QUICK_REACTIONS } from '../src/render/reactions';
import { expectedMine, singleReaction } from '../src/store/reactions';
import { canEdit, canRevoke } from '../src/util/msgOps';
import { accountLabel, fromChoice, parseNewAddress, phoneLimitHint } from '../src/util/newMessage';
import { peerReadMark } from '../src/util/peerRead';
import { sourceBadge, sourceLabel } from '../src/util/people';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 1,
    ext_id: '3EB0AAAA',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: true,
    kind: 'text',
    text: 'моё',
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
  } as Message;
}

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 5,
    ext_id: '555@lid',
    kind: 'dm',
    title: 'Петя',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 1,
    first_seen_ts: 0,
    unread: 0,
    project_id: 1,
    source: 'whatsapp',
    peer_read_sort: null,
    ...overrides,
  } as Channel;
}

function account(overrides: Partial<Account>): Account {
  return {
    id: 1,
    source: 'telegram',
    title: 'Личный',
    status: 'ok',
    last_ok_ts: null,
    last_error: null,
    flood_until: null,
    new_message: true,
    address: null,
    tg_phone_daily: 5,
    ...overrides,
  } as Account;
}

describe('Ф62-1: WhatsApp в интерфейсе', () => {
  it('значок и слово источника', () => {
    expect(sourceBadge('whatsapp')).toBe('WA');
    expect(sourceLabel('whatsapp')).toBe('WhatsApp');
    expect(accountLabel(account({ source: 'whatsapp', title: '+7 900' }))).toBe('WhatsApp · +7 900');
  });

  it('«✓✓» по времени: маркер ядра — время прочитанного сообщения', () => {
    const ch = channel({ peer_read_sort: 1500 });
    expect(peerReadMark(msg({ ts: 1500 }), ch)).toBe('read');
    expect(peerReadMark(msg({ ts: 1600 }), ch)).toBe('sent');
    expect(peerReadMark(msg({ ts: 1000 }), channel({ peer_read_sort: null }))).toBe('sent');
    expect(peerReadMark(msg({ ts: 1000, is_me: false }), ch)).toBeNull();
  });

  it('реакции и правка/удаление доступны', () => {
    expect(isReactionSource('whatsapp')).toBe(true);
    expect(QUICK_REACTIONS.whatsapp).toContain('👍');
    const ch = channel({});
    expect(canEdit(msg({}), ch)).toBe(true);
    expect(canRevoke(msg({}), ch)).toBe(true);
    // Подпись к файлу WhatsApp из хаба не правится — только «удалить у всех».
    const photo = msg({ kind: 'file', text: null, attachments_json: '[{"type":"photo"}]' });
    expect(canEdit(photo, ch)).toBe(false);
    expect(canRevoke(photo, ch)).toBe(true);
  });

  it('новый адресат по номеру — аккаунты Telegram и WhatsApp', () => {
    const address = parseNewAddress('+7 900 000-00-01');
    expect(address).toEqual({ kind: 'tg_phone', phone: '+7 900 000-00-01' });
    const accounts = [account({ id: 1 }), account({ id: 2, source: 'whatsapp' }), account({ id: 3, source: 'slack' })];
    const choice = fromChoice({ type: 'new', address: address! }, accounts);
    expect(choice.accounts.map((a) => a.id)).toEqual([1, 2]);
    expect(choice.preset).toBeNull();
    // У WhatsApp поиск по номеру выключен (0) — аккаунт не предлагается.
    const off = fromChoice({ type: 'new', address: address! }, [account({ id: 2, source: 'whatsapp', tg_phone_daily: 0 })]);
    expect(off.accounts).toEqual([]);
  });

  it('по номеру при одном Telegram — подставлен он, как раньше', () => {
    const address = parseNewAddress('+79000000001')!;
    const only = fromChoice({ type: 'new', address }, [account({ id: 1 }), account({ id: 3, source: 'slack' })]);
    expect(only).toMatchObject({ preset: 1, fixed: false });
    expect(only.accounts.map((a) => a.id)).toEqual([1]);
  });

  it('предел по номеру — у выбранного аккаунта; не выбран — наибольший', () => {
    const accounts = [account({ id: 1, tg_phone_daily: 5 }), account({ id: 2, source: 'whatsapp', tg_phone_daily: 2 })];
    expect(phoneLimitHint(accounts, 2)).toEqual({ daily: 2, enabled: true });
    expect(phoneLimitHint(accounts, 1)).toEqual({ daily: 5, enabled: true });
    expect(phoneLimitHint(accounts)).toEqual({ daily: 5, enabled: true });
    expect(phoneLimitHint([account({ id: 2, source: 'whatsapp', tg_phone_daily: 0 })], 2)).toEqual({
      daily: 0,
      enabled: false,
    });
  });

  it('одна реакция на человека — новая заменяет прежнюю', () => {
    expect(singleReaction('whatsapp')).toBe(true);
    expect(singleReaction('telegram')).toBe(true);
    expect(singleReaction('slack')).toBe(false);
    const ops = [
      { id: 1, emoji: '👍', on: true, at: 0 },
      { id: 2, emoji: '🔥', on: true, at: 0 },
    ];
    expect([...expectedMine('whatsapp', [], ops)]).toEqual([...expectedMine('telegram', [], ops)]);
    expect(expectedMine('whatsapp', [], ops).size).toBe(1);
  });
});
