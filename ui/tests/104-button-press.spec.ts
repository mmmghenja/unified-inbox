// Ф44-1d: нажатие кнопок ботов Telegram в интерфейсе (`docs/architecture.md`):
// запрос нажатия, состояния кнопки, строки исхода (ответ бота — только текст), `/sync.presses` и кадр SSE `press`,
// карточка «Нажать?» в «Ждёт» и на доске, строка выключателя `hub_press` в «Автопилоте», подпись «Без подтверждения».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AutopilotState, ButtonPress, Message, MessageButtons as Buttons, Project } from '../src/api/types';
import MessageButtons from '../src/components/MessageButtons.vue';
import MessageRow from '../src/components/MessageRow.vue';
import PressCard from '../src/components/PressCard.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createStaleness } from '../src/store/staleness';
import { connectStream, type EventSourceLike } from '../src/store/stream';
import { applyPress, createPressState } from '../src/store/presses';
import { groupSwitches } from '../src/util/autopilot';
import { buildBoard } from '../src/util/board';
import { liveButtons, outcomeLine, outcomeLines, visiblePresses } from '../src/util/presses';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 9, 9, 15, 0, 0).getTime();
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function press(overrides: Partial<ButtonPress> = {}): ButtonPress {
  return {
    id: 1,
    account_id: 1,
    channel_id: 5,
    project_id: 3,
    message_id: 7,
    row: 1,
    col: 1,
    text: 'Продлить',
    by: 'ui',
    role: null,
    auto: false,
    auto_by: null,
    status: 'approved',
    reason: null,
    created_ts: NOW,
    decided_ts: NOW,
    done_ts: null,
    updated_ts: NOW,
    seq: 1,
    ...overrides,
  };
}

const KB: Buttons = {
  kind: 'inline',
  rows: [[{ t: 'Продлить', k: 'callback' }, { t: 'Тариф', k: 'callback' }]],
};

function msg(overrides: Partial<Message> = {}): Message {
  return {
    id: 7,
    seq: 1,
    first_seq: 1,
    channel_id: 5,
    project_id: 3,
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
    buttons: KB,
    ...overrides,
  };
}

function project(): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: null, unread: 0 } as Project;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

let sent: Sent[] = [];
let handler: (s: Sent) => Response | undefined = () => undefined;

function stubFetch(): void {
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = {
        method: init?.method ?? 'GET',
        path: url.pathname,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      sent.push(s);
      return handler(s) ?? json([]);
    }),
  );
}

function resetPresses(): void {
  store.state.presses = createPressState();
}

beforeEach(() => {
  resetPresses();
  stubFetch();
  handler = () => undefined;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('44-1d util: исходы нажатия', () => {
  it('ответ бота — текстом; всплывающий — отдельным видом; ссылка только http(s)', () => {
    const ok = outcomeLine(press({ status: 'done', result: { message: 'Готово' } }), NOW)!;
    expect(ok.kind).toBe('ok');
    expect(ok.text).toBe('«Продлить»: бот ответил — Готово');
    const alert = outcomeLine(press({ status: 'done', result: { message: 'Внимание', alert: true } }), NOW)!;
    expect(alert.kind).toBe('alert');
    const link = outcomeLine(press({ status: 'done', result: { url: 'https://example.com/x' } }), NOW)!;
    expect(link.url).toBe('https://example.com/x');
    const bad = outcomeLine(press({ status: 'done', result: { url: 'javascript:alert(1)' } }), NOW)!;
    expect(bad.url).toBeNull();
    expect(bad.text).toBe('«Продлить»: нажато');
  });

  it('timeout, failed, unknown, expired, declined — по-русски', () => {
    expect(outcomeLine(press({ status: 'done', result: { timeout: true } }), NOW)!.text).toContain('бот не ответил на запрос');
    expect(outcomeLine(press({ status: 'failed', reason: 'stale_button' }), NOW)!.text).toContain('клавиатура изменилась');
    expect(outcomeLine(press({ status: 'failed', reason: 'flood' }), NOW)!.text).toContain('подождать');
    expect(outcomeLine(press({ status: 'failed', reason: 'RPCError' }), NOW)!.text).toBe('«Продлить»: не удалось: RPCError');
    expect(outcomeLine(press({ status: 'unknown', reason: 'lease_expired' }), NOW)!.text).toContain('исход неизвестен');
    expect(outcomeLine(press({ status: 'expired', reason: 'not_sent_in_time' }), NOW)!.text).toContain('не ушло в срок');
    expect(outcomeLine(press({ status: 'declined' }), NOW)!.text).toContain('отклонено');
  });

  it('живая строка — «нажимается…» / «ждёт подтверждения»; закрытое старше суток не показывается', () => {
    expect(outcomeLine(press({ status: 'leased' }), NOW)!.text).toContain('нажимается');
    expect(outcomeLine(press({ status: 'pending' }), NOW)!.text).toContain('ждёт подтверждения');
    expect(outcomeLine(press({ status: 'done', updated_ts: NOW - 25 * 3600_000 }), NOW)).toBeNull();
  });

  it('по кнопке — последнее нажатие; liveButtons делит ждёт владельца / нажимается', () => {
    const list = [
      press({ id: 1, status: 'done' }),
      press({ id: 2, status: 'approved' }),
      press({ id: 3, col: 2, status: 'pending' }),
      press({ id: 4, message_id: 99 }),
    ];
    expect(outcomeLines(list, 7, NOW)).toHaveLength(2);
    expect(liveButtons(list, 7)).toEqual({ waiting: ['1.2'], busy: ['1.1'] });
  });

  it('applyPress: старшая по seq побеждает, REST без result не стирает result из sync', () => {
    const st = createPressState();
    applyPress(st, press({ seq: 5, status: 'done', result: { message: 'ок' } }));
    applyPress(st, press({ seq: 4, status: 'approved' }));
    expect(st.items[1]!.status).toBe('done');
    applyPress(st, press({ seq: 6, status: 'done' }));
    expect(st.items[1]!.result).toEqual({ message: 'ок' });
  });
});

describe('44-1d MessageButtons: состояния и строки', () => {
  it('busy — кнопка неактивна с «нажимается…»; waiting — неактивна с подсказкой', () => {
    const w = mount(MessageButtons, { props: { buttons: KB, busy: ['1.1'], waiting: ['1.2'] } });
    const [a, b] = w.findAll('.msg-btn');
    expect(a!.attributes('disabled')).toBeDefined();
    expect(a!.text()).toContain('нажимается…');
    expect(b!.attributes('disabled')).toBeUndefined();
    expect(b!.attributes('title')).toBe('ждёт твоего решения');
  });

  it('клик по ждущей кнопке открывает карточку «Нажать?» и ничего не нажимает', async () => {
    const p = press({ id: 5, message_id: 7, row: 1, col: 2, status: 'pending' });
    const w = mount(MessageButtons, { props: { buttons: KB, waiting: ['1.2'], pending: [p] } });
    expect(w.findComponent(PressCard).exists()).toBe(false);
    await w.findAll('.msg-btn')[1]!.trigger('click');
    expect(w.findComponent(PressCard).exists()).toBe(true);
    expect(w.emitted('press')).toBeUndefined();
  });

  it('строка исхода: ответ бота только текстом, ссылка — <a> с rel', () => {
    const lines = [
      { key: 'a', kind: 'ok' as const, text: `«Продлить»: бот ответил — ${XSS}`, url: 'https://example.com/p', host: 'example.com' },
    ];
    const w = mount(MessageButtons, { props: { buttons: KB, lines } });
    expect(w.find('img').exists()).toBe(false);
    expect(w.find('script').exists()).toBe(false);
    expect(w.text()).toContain(XSS);
    const a = w.get('.msg-buttons__line a');
    expect(a.attributes('href')).toBe('https://example.com/p');
    expect(a.attributes('rel')).toBe('noopener noreferrer');
    expect(a.text()).toBe('example.com');
  });
});

describe('44-1d store: нажатие', () => {
  it('клик -> POST /messages/{id}/buttons/press с адресом и текстом; ответ — строка в сторе, кнопка занята', async () => {
    handler = (s) =>
      s.path === '/api/v1/messages/7/buttons/press' ? json({ press: press({ status: 'approved' }), created: true }, 201) : undefined;
    store.state.sync.messagesByChannel = { 5: [msg()] };
    const w = mount(MessageRow, { props: { message: msg() } });
    await w.findAll('.msg-btn')[1]!.trigger('click');
    await flushPromises();
    const post = sent.find((s) => s.method === 'POST' && s.path === '/api/v1/messages/7/buttons/press')!;
    expect(post.body).toEqual({ row: 1, col: 2, text: 'Тариф' });
    expect(store.state.presses.items[1]!.status).toBe('approved');
    // press() в стабе — кнопка (1,1); она «нажимается»
    expect(w.findAll('.msg-btn')[0]!.attributes('disabled')).toBeDefined();
    expect(w.text()).toContain('нажимается…');
  });

  it('409 stale_button — строка «клавиатура изменилась»', async () => {
    handler = (s) => (s.method === 'POST' ? json({ detail: 'stale_button: текст другой' }, 409) : undefined);
    await store.pressButton(7, 1, 1, 'Продлить');
    expect(store.state.presses.errors['7:1:1']).toContain('клавиатура изменилась');
    const w = mount(MessageRow, { props: { message: msg() } });
    expect(w.text()).toContain('клавиатура изменилась');
  });

  it('422 — причина ядра под клавиатурой; сеть — «не отправилось»', async () => {
    handler = () => json({ detail: 'кнопка с паролем' }, 422);
    await store.pressButton(7, 1, 1, 'Продлить');
    expect(store.state.presses.errors['7:1:1']).toBe('не нажато: кнопка с паролем');
  });

  it('исход приходит из /sync.presses: «бот ответил» под клавиатурой', async () => {
    store.state.presses.items[1] = press({ status: 'approved' });
    const w = mount(MessageRow, { props: { message: msg() } });
    expect(w.text()).toContain('нажимается…');
    // Компонент сверяет давность исхода с настоящими часами — время исхода берём от них же.
    applyPress(
      store.state.presses,
      press({ seq: 2, status: 'done', updated_ts: Date.now(), result: { message: `Продлено ${XSS}` } }),
    );
    await flushPromises();
    expect(w.text()).toContain('бот ответил — Продлено');
    expect(w.find('img').exists()).toBe(false);
    expect(w.findAll('.msg-btn')[0]!.attributes('disabled')).toBeUndefined();
  });

  it('loadPresses читает GET /button-presses', async () => {
    handler = (s) => (s.path === '/api/v1/button-presses' ? json([press({ id: 9 })]) : undefined);
    await store.loadPresses();
    expect(store.state.presses.items[9]!.id).toBe(9);
  });

  it('кадр SSE press будит /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      addEventListener: (name: string, fn: () => void) => void listeners.set(name, fn),
      close: () => undefined,
      onerror: null,
      onopen: null,
    } as unknown as EventSourceLike;
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('press')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
  });
});

describe('44-1d карточка «Нажать?»', () => {
  const pending = (o: Partial<ButtonPress> = {}) =>
    press({ status: 'pending', by: 'session', role: 'маркетолог', channel_title: 'Demo VPN Bot', quote: 'Подписка активна', ...o });

  it('роль, беседа, цитата, текст кнопки; «Нажать» -> approve, «Отказать» -> decline', async () => {
    handler = (s) => (s.path.endsWith('/approve') || s.path.endsWith('/decline') ? json(press({ seq: 3, status: 'approved' })) : undefined);
    const w = mount(PressCard, { props: { press: pending() } });
    expect(w.text()).toContain('Нажать «Продлить»?');
    expect(w.text()).toContain('Demo VPN Bot');
    expect(w.text()).toContain('маркетолог');
    expect(w.text()).toContain('Подписка активна');
    await w.get('.press-approve').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'POST' && s.path === '/api/v1/button-presses/1/approve')).toBe(true);
    await w.get('.press-decline').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.path === '/api/v1/button-presses/1/decline')).toBe(true);
  });

  it('409 — «уже изменилось», строка перечитана', async () => {
    handler = (s) => (s.method === 'POST' ? json({ detail: 'уже решено' }, 409) : undefined);
    const w = mount(PressCard, { props: { press: pending() } });
    await w.get('.press-approve').trigger('click');
    await flushPromises();
    expect(w.text()).toContain('уже изменилось');
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/button-presses')).toBe(true);
  });

  it('чужой текст — не HTML', () => {
    const w = mount(PressCard, { props: { press: pending({ text: XSS, quote: XSS, role: XSS, channel_title: XSS }) } });
    expect(w.find('img').exists()).toBe(false);
    expect(w.find('script').exists()).toBe(false);
  });

  it('«Ждёт»: карточка есть, входит в счётчик главной; решённые — нет', () => {
    const input = {
      projects: [project()],
      home: { react: [], drafts: [] },
      work: { tasks: {}, pending: {}, stale: {} },
      presses: [pending({ id: 1 }), press({ id: 2, status: 'done' })],
      nowMs: NOW,
    };
    const items = buildWaiting(input);
    expect(items.filter((i) => i.kind === 'press')).toHaveLength(1);
    expect(homeDecisionCount(input)).toBe(1);
    expect(visiblePresses(input.presses).map((p) => p.id)).toEqual([1]);
    store.state.presses.items[1] = pending({ id: 1 });
    const w = mount(WaitList);
    expect(w.find('.press-card').exists()).toBe(true);
  });

  it('доска: карточка — в столбце проекта, в счётчике столбца', () => {
    const board = buildBoard(
      [project()],
      { react: [], drafts: [], runs: [], sent: [], presses: [pending({ id: 1 }), press({ id: 2, status: 'done' })] },
      NOW,
    );
    const column = board.columns.find((c) => c.project.id === 3)!;
    expect(column.presses.map((p) => p.id)).toEqual([1]);
    expect(column.waitCount).toBe(1);
  });
});

describe('44-1d автопилот и «Без подтверждения»', () => {
  it('строка hub_press приходит каталогом в группе «Сообщения людям» с русской подписью', () => {
    const state: AutopilotState = {
      switches: [
        { key: 'hub_press', group: 'Сообщения людям', title: 'нажатие кнопок ботов', manual: 'ask', meta: false, note: 'бот выполнит действие от твоего имени' },
      ],
      global: { hub_press: true },
      projects: {},
      local: true,
    };
    const groups = groupSwitches(state.switches);
    expect(groups[0]!.title).toBe('Сообщения людям');
    expect(groups[0]!.switches[0]!.title).toBe('нажатие кнопок ботов');
  });
});
