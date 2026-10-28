// F5 (приёмка 6b, дефект найден чтением кода): «нижнее видимое сообщение» — по ПОРЯДКУ ПОКАЗА
// (ts, ext_sort, id), не по максимальному `id` (id не монотонен относительно порядка: догрузка истории
// может вставить строку со старым ts, но бóльшим id, чем у уже показанных выше). Чистая функция, без DOM.
import { describe, expect, it } from 'vitest';

import type { Message } from '../src/api/types';
import { findBottomVisibleMessage, isNewerInDisplayOrder } from '../src/store/visibleMessage';

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
    text: 'x',
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

describe('F5 findBottomVisibleMessage: по позиции в отсортированном массиве, не по max(id)', () => {
  it('история, догруженная позже, получает БОЛЬШИЙ id, но МЕНЬШИЙ ts — и стоит выше в массиве', () => {
    // Порядок показа (как в sync.messagesByChannel после applySync): старое ts выше, новое — ниже.
    // id=50 — старое сообщение, догруженное reconcile'ом ПОЗЖЕ (поэтому больший внутренний id).
    const messages: Message[] = [
      msg({ id: 50, ts: 500 }), // старая история, id большой, но ts маленький — стоит первой (выше)
      msg({ id: 10, ts: 1000 }), // настоящее нижнее сообщение по порядку показа
    ];
    const visible = new Set([50, 10]);
    const bottom = findBottomVisibleMessage(messages, visible);
    // «max(id)» дал бы 50 (неверно) — правильный ответ по позиции в массиве это id=10.
    expect(bottom?.id).toBe(10);
  });

  it('если видим только более раннее по порядку показа сообщение — оно и есть «нижнее видимое»', () => {
    const messages: Message[] = [msg({ id: 50, ts: 500 }), msg({ id: 10, ts: 1000 })];
    const bottom = findBottomVisibleMessage(messages, new Set([50]));
    expect(bottom?.id).toBe(50);
  });

  it('ничего не видно -> null', () => {
    const messages: Message[] = [msg({ id: 1, ts: 100 })];
    expect(findBottomVisibleMessage(messages, new Set())).toBeNull();
  });

  it('пустая лента -> null', () => {
    expect(findBottomVisibleMessage([], new Set([1]))).toBeNull();
  });
});

describe('F5 isNewerInDisplayOrder: не слать read назад', () => {
  it('previous=null -> всегда true (первая отметка)', () => {
    expect(isNewerInDisplayOrder(msg({ id: 1, ts: 100 }), null)).toBe(true);
  });

  it('candidate раньше previous по порядку показа -> false (не откатываем read)', () => {
    const previous = msg({ id: 10, ts: 1000 });
    const candidate = msg({ id: 50, ts: 500 }); // догруженная история, "видимая" после прокрутки вверх
    expect(isNewerInDisplayOrder(candidate, previous)).toBe(false);
  });

  it('candidate строго позже previous -> true', () => {
    const previous = msg({ id: 10, ts: 1000 });
    const candidate = msg({ id: 11, ts: 1001 });
    expect(isNewerInDisplayOrder(candidate, previous)).toBe(true);
  });

  it('candidate === previous по порядку (тот же id) -> false', () => {
    const m = msg({ id: 10, ts: 1000 });
    expect(isNewerInDisplayOrder(m, m)).toBe(false);
  });
});
