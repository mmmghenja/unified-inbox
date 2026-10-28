// Пункт 7 спеки: чистый редьюсер `applySync` — без DOM, без Vue.
import { describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import { applySync, compareMessages, createSyncState } from '../src/store/applySync';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 10,
    project_id: 1,
    ext_id: null,
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'hello',
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

describe('07 applySync: порядок (ts, ext_sort, id)', () => {
  it('сортирует по числовому ext_id при равных ts', () => {
    const state = createSyncState();
    // Вперемешку — редьюсер обязан отсортировать сам, не полагаясь на порядок прихода.
    const messages = [102, 100, 98, 101, 99].map((extIdNum, i) =>
      msg({ id: 900 + i, ext_id: String(extIdNum), ts: 5000 }),
    );
    applySync(state, { messages, channels: [], max_seq: 10 });

    const order = state.messagesByChannel[10]!.map((m) => m.ext_id);
    expect(order).toEqual(['98', '99', '100', '101', '102']);
  });

  it('заготовка (ext_id=null) сортируется как ext_sort=0 — раньше положительных ext_id при равном ts', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [
        msg({ id: 1, ext_id: '5', ts: 1000 }),
        msg({ id: 2, ext_id: null, ts: 1000 }),
      ],
      channels: [],
      max_seq: 2,
    });
    const order = state.messagesByChannel[10]!.map((m) => m.id);
    expect(order).toEqual([2, 1]);
  });

  it('при равных ts и ext_sort — тай-брейк по id', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [
        msg({ id: 20, ext_id: null, ts: 1000 }),
        msg({ id: 10, ext_id: null, ts: 1000 }),
      ],
      channels: [],
      max_seq: 2,
    });
    const order = state.messagesByChannel[10]!.map((m) => m.id);
    expect(order).toEqual([10, 20]);
  });
});

describe('07 applySync: правка/удаление/реакция — upsert по id, без дублей', () => {
  it('правка текста заменяет строку той же id', () => {
    const state = createSyncState();
    applySync(state, { messages: [msg({ id: 1, text: 'исходный' })], channels: [], max_seq: 1 });
    applySync(state, {
      messages: [msg({ id: 1, text: 'исправленный', edited_ts: 2000 })],
      channels: [],
      max_seq: 2,
    });
    const list = state.messagesByChannel[10]!;
    expect(list).toHaveLength(1);
    expect(list[0]!.text).toBe('исправленный');
    expect(list[0]!.edited_ts).toBe(2000);
  });

  it('удаление отмечает deleted и гасит текст, без второй строки', () => {
    const state = createSyncState();
    applySync(state, { messages: [msg({ id: 1, text: 'секрет' })], channels: [], max_seq: 1 });
    applySync(state, {
      messages: [msg({ id: 1, text: null, deleted: true })],
      channels: [],
      max_seq: 2,
    });
    const list = state.messagesByChannel[10]!;
    expect(list).toHaveLength(1);
    expect(list[0]!.deleted).toBe(true);
    expect(list[0]!.text).toBeNull();
  });

  it('реакция обновляет ту же строку', () => {
    const state = createSyncState();
    applySync(state, { messages: [msg({ id: 1, reactions_json: null })], channels: [], max_seq: 1 });
    applySync(state, {
      messages: [msg({ id: 1, reactions_json: '[{"emoji":"👍","count":1}]' })],
      channels: [],
      max_seq: 2,
    });
    const list = state.messagesByChannel[10]!;
    expect(list).toHaveLength(1);
    expect(list[0]!.reactions_json).toBe('[{"emoji":"👍","count":1}]');
  });
});

describe('07 applySync: заготовка исходящего -> настоящая строка, без дубля', () => {
  it('ext_id меняется с null на реальный на ТОЙ ЖЕ id (склейка эха на сервере) — одна строка в ленте', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [msg({ id: 5, ext_id: null, is_me: true, outbox_id: 5, outbox_status: 'queued' })],
      channels: [],
      max_seq: 1,
    });
    expect(state.messagesByChannel[10]).toHaveLength(1);

    applySync(state, {
      messages: [
        msg({ id: 5, ext_id: '3001', is_me: true, outbox_id: 5, outbox_status: 'sent' }),
      ],
      channels: [],
      max_seq: 2,
    });
    const list = state.messagesByChannel[10]!;
    expect(list).toHaveLength(1);
    expect(list[0]!.ext_id).toBe('3001');
    expect(list[0]!.outbox_status).toBe('sent');
  });
});

describe('07 applySync: догон `more` циклом', () => {
  it('несколько последовательных пачек накапливаются, lastSeq растёт', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [msg({ id: 1, ts: 100 }), msg({ id: 2, ts: 200 })],
      channels: [],
      max_seq: 5,
    });
    applySync(state, {
      messages: [msg({ id: 3, ts: 300 }), msg({ id: 4, ts: 400 }), msg({ id: 5, ts: 500 })],
      channels: [],
      max_seq: 10,
    });
    expect(state.messagesByChannel[10]).toHaveLength(5);
    expect(state.lastSeq).toBe(10);
  });
});

describe('07 applySync: повторное применение той же пачки — no-op', () => {
  it('идемпотентно: состав, порядок и lastSeq не меняются', () => {
    const state = createSyncState();
    const batch = {
      messages: [
        msg({ id: 1, ts: 100, ext_id: '5' }),
        msg({ id: 2, ts: 100, ext_id: '3' }),
        msg({ id: 3, ts: 200, text: 'x' }),
      ],
      channels: [
        {
          id: 10,
          ext_id: 'c1',
          kind: 'dm' as const,
          title: 'A',
          tier: null,
          muted: false,
          archived: false,
          map_reason: null,
          last_msg_ts: 200,
          my_msg_count: 1,
          first_seen_ts: 1,
          unread: 0,
          project_id: 1,
          seq: 3,
        },
      ],
      max_seq: 7,
    };
    applySync(state, batch);
    const snapshot = JSON.stringify(state);
    applySync(state, batch);
    expect(JSON.stringify(state)).toBe(snapshot);
  });
});

// Тест 13 спеки `docs/architecture.md`: `applySync` не кладёт ответ в ленту канала.
describe('07 applySync: раздел E спеки 2b — ответы Slack не попадают в ленту канала', () => {
  it('сообщение с thread_ext_id != ext_id (ответ) не добавляется в messagesByChannel', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [
        msg({ id: 1, ext_id: 'P1', ts: 1000, thread_ext_id: null }),
        msg({ id: 2, ext_id: 'R1', ts: 1001, thread_ext_id: 'P1' }),
      ],
      channels: [],
      max_seq: 2,
    });
    const ids = state.messagesByChannel[10]!.map((m) => m.ext_id);
    expect(ids).toEqual(['P1']);
  });

  it('родитель с thread_ext_id == ext_id (пришёл со сводкой) — не ответ, попадает в ленту', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [msg({ id: 1, ext_id: 'P1', ts: 1000, thread_ext_id: 'P1' })],
      channels: [],
      max_seq: 1,
    });
    expect(state.messagesByChannel[10]!.map((m) => m.ext_id)).toEqual(['P1']);
  });

  it('заготовка-ответ (ext_id=null, thread_ext_id задан) — тоже не в ленте канала', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [msg({ id: 1, ext_id: null, ts: 1000, thread_ext_id: 'P1' })],
      channels: [],
      max_seq: 1,
    });
    expect(state.messagesByChannel[10] ?? []).toEqual([]);
  });

  it('broadcast-ответ — тоже "ответ" по предикату, но кусок 2e кладёт его И в ленту (Slack показывает так же)', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [msg({ id: 1, ext_id: 'B1', ts: 1000, thread_ext_id: 'P1', thread_broadcast: true })],
      channels: [],
      max_seq: 1,
    });
    expect(state.messagesByChannel[10]?.map((m) => m.ext_id)).toEqual(['B1']);
  });
});

// Тесты группы 1 спеки `docs/architecture.md`: тред-хранилище стора наполняется ТОЛЬКО для
// уже загруженного (открытого) треда; broadcast — в обоих местах; повтор пачки — без дублей.
describe('07 applySync: раздел «Стор» спеки 2e — тред-хранилище (threads)', () => {
  function withLoadedThread(channelId: number, threadExtId: string) {
    const state = createSyncState();
    state.threads[`${channelId}:${threadExtId}`] = {
      parent: null,
      replies: [],
      nextBefore: null,
      loading: false,
    };
    return state;
  }

  it('ответ ЗАГРУЖЕННОГО треда попадает в threads[key].replies, в ленту канала — нет', () => {
    const state = withLoadedThread(10, 'P1');
    applySync(state, {
      messages: [msg({ id: 2, ext_id: 'R1', ts: 1001, thread_ext_id: 'P1' })],
      channels: [],
      max_seq: 1,
    });
    expect(state.threads['10:P1']!.replies.map((m) => m.ext_id)).toEqual(['R1']);
    expect(state.messagesByChannel[10] ?? []).toEqual([]);
  });

  it('ответ НЕзагруженного треда не создаёт запись в threads вовсе', () => {
    const state = createSyncState();
    applySync(state, {
      messages: [msg({ id: 2, ext_id: 'R1', ts: 1001, thread_ext_id: 'P1' })],
      channels: [],
      max_seq: 1,
    });
    expect(state.threads['10:P1']).toBeUndefined();
    expect(state.messagesByChannel[10] ?? []).toEqual([]);
  });

  it('broadcast загруженного треда — в threads[key].replies И в ленту канала', () => {
    const state = withLoadedThread(10, 'P1');
    applySync(state, {
      messages: [msg({ id: 3, ext_id: 'B1', ts: 1002, thread_ext_id: 'P1', thread_broadcast: true })],
      channels: [],
      max_seq: 1,
    });
    expect(state.threads['10:P1']!.replies.map((m) => m.ext_id)).toEqual(['B1']);
    expect(state.messagesByChannel[10]?.map((m) => m.ext_id)).toEqual(['B1']);
  });

  it('родитель обновляет threads[key].parent, если его тред загружен', () => {
    const state = withLoadedThread(10, 'P1');
    applySync(state, {
      messages: [msg({ id: 1, ext_id: 'P1', ts: 1000, thread_ext_id: null, text: 'изменённый текст' })],
      channels: [],
      max_seq: 1,
    });
    expect(state.threads['10:P1']!.parent?.text).toBe('изменённый текст');
    expect(state.messagesByChannel[10]?.map((m) => m.ext_id)).toEqual(['P1']);
  });

  it('повтор пачки идемпотентен: состав threads не меняется, без дублей', () => {
    const state = withLoadedThread(10, 'P1');
    const batch = {
      messages: [msg({ id: 2, ext_id: 'R1', ts: 1001, thread_ext_id: 'P1' })],
      channels: [],
      max_seq: 1,
    };
    applySync(state, batch);
    applySync(state, batch);
    expect(state.threads['10:P1']!.replies).toHaveLength(1);
  });
});

// Находки opus-критика 2b (U1/U2), закрытые в кусок 2e — дополнение к заданию архитектора.
describe('07 applySync: U1 — broadcast появляется И обновляется в ленте (не только при первом приходе)', () => {
  it('правка broadcast-ответа обновляет ту же строку и в ленте, и в треде, без дублей', () => {
    const state = createSyncState();
    state.threads['10:P1'] = { parent: null, replies: [], nextBefore: null, loading: false };

    applySync(state, {
      messages: [
        msg({ id: 4, ext_id: 'B1', ts: 1002, thread_ext_id: 'P1', thread_broadcast: true, text: 'исходный' }),
      ],
      channels: [],
      max_seq: 1,
    });
    expect(state.messagesByChannel[10]?.find((m) => m.id === 4)?.text).toBe('исходный');

    applySync(state, {
      messages: [
        msg({
          id: 4,
          ext_id: 'B1',
          ts: 1002,
          thread_ext_id: 'P1',
          thread_broadcast: true,
          text: 'исправленный',
          edited_ts: 2000,
        }),
      ],
      channels: [],
      max_seq: 2,
    });

    const lenta = state.messagesByChannel[10]!;
    expect(lenta.filter((m) => m.id === 4)).toHaveLength(1); // без дублей
    expect(lenta.find((m) => m.id === 4)?.text).toBe('исправленный');
    expect(state.threads['10:P1']!.replies.filter((m) => m.id === 4)).toHaveLength(1);
    expect(state.threads['10:P1']!.replies.find((m) => m.id === 4)?.text).toBe('исправленный');
  });
});

describe('07 applySync: U2 — порядок ответов Slack с одинаковым ts (мс) идёт по микросекундам ext_id, не по id', () => {
  it('compareMessages: сообщение с бОльшим id, но МЕНЬШИМИ микросекундами ts Slack — раньше по порядку показа', () => {
    // Одна и та же миллисекунда `ts`; при неверном (числовом-only) `extSortOf` обе строки дали бы ext_sort=0
    // и сортировались бы по `id` — тогда `b` (id=3) оказался бы РАНЬШЕ `a` (id=10), что противоречит серверу
    // (`core/extsort.py::ext_sort`, находка U2): `a` (…000100) обязан идти раньше `b` (…000900).
    const a = msg({ id: 10, ext_id: '1700000000.000100', ts: 1700000000123, thread_ext_id: null });
    const b = msg({ id: 3, ext_id: '1700000000.000900', ts: 1700000000123, thread_ext_id: null });
    expect(compareMessages(a, b)).toBeLessThan(0);
    expect(compareMessages(b, a)).toBeGreaterThan(0);
  });

  it('применённые через applySync ответы одного треда с одинаковым ts сортируются по микросекундам ext_id', () => {
    const state = createSyncState();
    state.threads['10:P1'] = { parent: null, replies: [], nextBefore: null, loading: false };
    const SAME_TS_MS = 1700000000123;
    applySync(state, {
      messages: [
        // Порядок прихода/id намеренно «неправильный» относительно микросекунд — чтобы падал именно на
        // сортировке по ext_sort, а не совпадал с id-порядком случайно.
        msg({ id: 10, ext_id: '1700000000.000900', ts: SAME_TS_MS, thread_ext_id: 'P1' }),
        msg({ id: 3, ext_id: '1700000000.000100', ts: SAME_TS_MS, thread_ext_id: 'P1' }),
      ],
      channels: [],
      max_seq: 1,
    });
    const order = state.threads['10:P1']!.replies.map((m) => m.ext_id);
    expect(order).toEqual(['1700000000.000100', '1700000000.000900']);
  });

  it('мусорный/нечисловой ext_id и Telegram-числа — как раньше (регресс)', () => {
    const state = createSyncState();
    state.threads['10:P1'] = { parent: null, replies: [], nextBefore: null, loading: false };
    applySync(state, {
      messages: [
        msg({ id: 1, ext_id: 'not-a-number', ts: 1000, thread_ext_id: 'P1' }),
        msg({ id: 2, ext_id: '42', ts: 1000, thread_ext_id: 'P1' }),
      ],
      channels: [],
      max_seq: 1,
    });
    // ext_sort мусорного ext_id — 0, значит он раньше '42' (ext_sort=42) при равном ts.
    expect(state.threads['10:P1']!.replies.map((m) => m.ext_id)).toEqual(['not-a-number', '42']);
  });
});
