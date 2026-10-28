// Ф41-3а (`docs/architecture.md`): шаги окна сессии по `brief` (diff «✓ файл +N −M»,
// вывод «готово · N строк»), ленивое раскрытие подробностей одним запросом `GET /sessions/{id}/events/{event_id}`,
// показ diff и вывода, конец хода со временем и ценой, строка сессии с контекстом и порогами, «Усыпить» у спящей.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, SessionEvent, SessionEventDetail, SessionUsage } from '../src/api/types';
import SessionCacheLine from '../src/components/SessionCacheLine.vue';
import SessionsBlock from '../src/components/SessionsBlock.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import { store } from '../src/store/instance';
import { createSessionsState, mergeSessionEvents } from '../src/store/sessions';
import { costLabel, durationLabel, parseDiff, stepView, usageLabel } from '../src/util/sessionSteps';

const NOW = new Date(2026, 10, 8, 15, 0, 0).getTime();

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
}

/** Ответы ядра по контракту спеки: список событий (пустой), ручка одного события — `details[id]`. */
function stubFetch(
  details: Record<number, SessionEventDetail | null> = {},
  failFirst = false,
  listed: HubSession[] = [],
): { sent: Sent[] } {
  const sent: Sent[] = [];
  let failed = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      sent.push({ method, path: url.pathname });
      const one = /^\/api\/v1\/sessions\/5\/events\/(\d+)$/.exec(url.pathname);
      if (one !== null) {
        if (failFirst && !failed) {
          failed = true;
          return new Response('x', { status: 500 });
        }
        const id = Number(one[1]);
        return jsonResponse({ ...storeEvent(id), detail: details[id] ?? null });
      }
      if (/^\/api\/v1\/sessions\/\d+\/events$/.test(url.pathname)) return jsonResponse([]);
      if (/^\/api\/v1\/sessions\/\d+$/.test(url.pathname)) return jsonResponse({ session: session({}), requests: [], says: [] });
      if (url.pathname === '/api/v1/sessions') return jsonResponse({ items: listed });
      if (url.pathname === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
      return jsonResponse({});
    }),
  );
  return { sent };
}

function storeEvent(id: number): SessionEvent {
  return store.state.sessions.events[5]!.find((e) => e.id === id)!;
}

function detailRequests(sent: Sent[]): Sent[] {
  return sent.filter((r) => /\/events\/\d+$/.test(r.path));
}

function session(overrides: Partial<HubSession>): HubSession {
  return {
    id: 5,
    project_id: 3,
    role: 'программист',
    role_no: 1,
    engine: 'claude',
    model: 'sonnet',
    effort: null,
    mode: 'manual',
    title: null,
    state: 'idle',
    host_seen_ts: NOW,
    created_ts: NOW - 1000,
    updated_ts: NOW - 500,
    closed_ts: null,
    close_reason: null,
    created_by: 'ui',
    seq: 10,
    last_event_id: 20,
    last_turn_ts: null,
    failure: null,
    wait_reason: null,
    pending_requests: 0,
    queued_says: 0,
    interrupt_requested: false,
    sleep_requested: false,
    sub_id: null,
    listening: false,
    engine_session_id: null,
    workdir: '/home/owner/proj',
    cache: null,
    usage: null,
    ...overrides,
  };
}

function ev(overrides: Partial<SessionEvent>): SessionEvent {
  return { id: 1, ts: NOW, kind: 'note' as SessionEvent['kind'], tool: null, summary: '', is_error: false, ref: null, ...overrides };
}

const PATCH = ['--- a/src/x.ts', '+++ b/src/x.ts', '@@ -1,2 +1,2 @@', ' keep', '-old line', '+new line'].join('\n');

function mountWindow(events: SessionEvent[], overrides: Partial<HubSession> = {}) {
  const s = session(overrides);
  store.state.sessions.items[s.id] = s;
  store.state.sessions.events[s.id] = events;
  const w = mount(SessionWindow, { props: { session: s } });
  // Шаги инструментов в окне свёрнуты одной строкой — тесты подробностей смотрят раскрытую пачку.
  for (const b of w.findAll('.tools-toggle')) void b.trigger('click');
  return w;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sessions = createSessionsState();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-3а: строки шагов по brief (чистые функции)', () => {
  it('diff: «✓ файл +N −M»; вывод: «готово · N строк»; ошибка — как прежде', () => {
    const diff = stepView(ev({ kind: 'tool_done', summary: 'готово', brief: { type: 'diff', file: 'src/x.ts', added: 3, removed: 1 }, has_detail: true }));
    expect(diff).toMatchObject({ text: '✓ src/x.ts +3 −1', tone: 'done', expandable: true });
    const out = stepView(ev({ kind: 'tool_done', summary: 'готово (157 симв.)', brief: { type: 'output', lines: 12 }, has_detail: true }));
    expect(out.text).toBe('готово · 12 строк');
    expect(stepView(ev({ kind: 'tool_done', summary: 'x', brief: { type: 'output', lines: 1 } })).text).toBe('готово · 1 строка');
    const err = stepView(ev({ kind: 'tool_done', is_error: true, summary: 'упало', brief: { type: 'output', lines: 4 }, has_detail: true }));
    expect(err).toMatchObject({ text: '✗ упало', tone: 'error', expandable: true });
  });

  it('событие без brief и без подробностей рисуется как раньше', () => {
    const v = stepView(ev({ kind: 'tool_done', summary: 'готово (157 симв.)' }));
    expect(v).toMatchObject({ text: 'готово (157 симв.)', tone: 'done', expandable: false });
    expect(stepView(ev({ kind: 'tool', tool: 'Edit', summary: 'src/x.ts' })).text).toBe('▸ Edit: src/x.ts');
  });

  it('конец хода: время, шаги, цена; цена меньше цента и null', () => {
    const usage = (cost: number | null, ms = 12_000, steps = 5) =>
      stepView(ev({ kind: 'turn_end', summary: '12 с', brief: { type: 'usage', duration_ms: ms, steps, cost_usd: cost, tokens: null } })).text;
    expect(usage(0.03)).toBe('ход окончен · 12 с · 5 шагов · $0.03');
    expect(usage(0.004)).toBe('ход окончен · 12 с · 5 шагов · < $0.01');
    expect(usage(null, 65_000, 1)).toBe('ход окончен · 1 мин 5 с · 1 шаг');
    expect(durationLabel(300)).toBe('< 1 с');
    expect(costLabel(1.2)).toBe('$1.20');
  });

  it('usageLabel: контекст из предела, пороги 70 % и 90 %, цена', () => {
    const u = (tokens: number, limit: number | null, cost: number | null = null): SessionUsage => ({
      context_tokens: tokens,
      context_limit: limit,
      cost_usd: cost,
      updated_ts: NOW,
    });
    expect(usageLabel(u(84_000, 200_000, 1.2))).toEqual({ context: 'контекст 84 из 200 тыс.', contextTone: 'normal', cost: 'всего $1.20' });
    expect(usageLabel(u(140_001, 200_000)).contextTone).toBe('warn');
    expect(usageLabel(u(140_000, 200_000)).contextTone).toBe('normal');
    expect(usageLabel(u(180_001, 200_000)).contextTone).toBe('danger');
    expect(usageLabel(u(84_000, null))).toMatchObject({ context: 'контекст 84 тыс.', contextTone: 'normal', cost: null });
  });

  it('parseDiff: плюсы, минусы, @@, заголовки файла', () => {
    expect(parseDiff(`${PATCH}\n`).map((l) => l.kind)).toEqual(['meta', 'meta', 'hunk', 'ctx', 'del', 'add']);
  });
});

describe('Ф41-3а: окно сессии — раскрытие подробностей', () => {
  it('diff: строка «✓ файл +N −M»; клик — ОДИН запрос, плюсы и минусы разного вида; повторное раскрытие без запроса', async () => {
    const { sent } = stubFetch({ 11: { type: 'diff', file: 'src/x.ts', patch: PATCH, cut: true } });
    const w = mountWindow([
      ev({ id: 10, kind: 'tool', tool: 'Edit', summary: 'src/x.ts' }),
      ev({ id: 11, kind: 'tool_done', summary: 'готово', brief: { type: 'diff', file: 'src/x.ts', added: 1, removed: 1 }, has_detail: true }),
    ]);
    await flushPromises();
    const row = w.get('[data-event-id="11"]');
    expect(row.text()).toContain('✓ src/x.ts +1 −1');
    expect(detailRequests(sent)).toHaveLength(0);
    await row.get('button').trigger('click');
    await flushPromises();
    expect(detailRequests(sent)).toHaveLength(1);
    expect(detailRequests(sent)[0]!.path).toBe('/api/v1/sessions/5/events/11');
    expect(row.get('.diff-line--add').text()).toBe('+new line');
    expect(row.get('.diff-line--del').text()).toBe('-old line');
    expect(row.get('.diff-line--hunk').text()).toBe('@@ -1,2 +1,2 @@');
    expect(row.text()).toContain('diff обрезан');
    await row.get('button').trigger('click'); // свернуть
    expect(row.find('.diff-view').exists()).toBe(false);
    await row.get('button').trigger('click'); // раскрыть снова
    await flushPromises();
    expect(row.find('.diff-view').exists()).toBe(true);
    expect(detailRequests(sent)).toHaveLength(1);
    w.unmount();
  });

  it('вывод команды — в <pre> текстом, без разметки', async () => {
    stubFetch({ 12: { type: 'output', text: '<b>line1</b>\nline2', cut: false } });
    const w = mountWindow([
      ev({ id: 12, kind: 'tool_done', summary: 'готово (18 симв.)', brief: { type: 'output', lines: 2 }, has_detail: true }),
    ]);
    await flushPromises();
    const row = w.get('[data-event-id="12"]');
    expect(row.text()).toContain('готово · 2 строки');
    await row.get('button').trigger('click');
    await flushPromises();
    const pre = row.get('pre.step-pre');
    expect(pre.text()).toBe('<b>line1</b>\nline2');
    expect(pre.find('b').exists()).toBe(false);
    w.unmount();
  });

  it('полный вход инструмента: команда в <pre>, прочее — json', async () => {
    stubFetch({
      13: { type: 'input', command: 'git status --short && ls -la', description: 'статус' },
      14: { type: 'input', input: { pattern: 'foo', path: 'src' } },
    });
    const w = mountWindow([
      ev({ id: 13, kind: 'tool', tool: 'Bash', summary: 'git status', has_detail: true }),
      ev({ id: 14, kind: 'tool', tool: 'Grep', summary: 'foo', has_detail: true }),
    ]);
    await flushPromises();
    const bash = w.get('[data-event-id="13"]');
    // Приёмка 41-3: у раскрываемой строки значок один — свой; «▸» из текста шага убран.
    expect(bash.get('button .step-text').text()).toBe('Bash: git status');
    expect(bash.get('button .step-mark').text()).toBe('▸');
    await bash.get('button').trigger('click');
    await flushPromises();
    expect(bash.get('pre').text()).toBe('git status --short && ls -la');
    expect(bash.text()).toContain('статус');
    const grep = w.get('[data-event-id="14"]');
    await grep.get('button').trigger('click');
    await flushPromises();
    expect(JSON.parse(grep.get('pre').text())).toEqual({ pattern: 'foo', path: 'src' });
    w.unmount();
  });

  it('раскрытое не закрывается при догрузке событий', async () => {
    stubFetch({ 11: { type: 'output', text: 'abc', cut: false } });
    const w = mountWindow([
      ev({ id: 11, kind: 'tool_done', summary: 'готово', brief: { type: 'output', lines: 1 }, has_detail: true }),
    ]);
    await flushPromises();
    await w.get('[data-event-id="11"] button').trigger('click');
    await flushPromises();
    expect(w.find('pre.step-pre').exists()).toBe(true);
    mergeSessionEvents(store.state.sessions, 5, [ev({ id: 15, kind: 'text', summary: 'дальше', ref: 'm1:0' })]);
    await flushPromises();
    expect(w.findAll('[data-event-id]')).toHaveLength(2);
    expect(w.get('[data-event-id="11"] pre.step-pre').text()).toBe('abc');
    w.unmount();
  });

  it('ошибка загрузки подробностей — строка и повтор по следующему клику', async () => {
    const { sent } = stubFetch({ 11: { type: 'output', text: 'ok', cut: false } }, true);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const w = mountWindow([
      ev({ id: 11, kind: 'tool_done', summary: 'готово', brief: { type: 'output', lines: 1 }, has_detail: true }),
    ]);
    await flushPromises();
    const btn = w.get('[data-event-id="11"] button');
    await btn.trigger('click');
    await flushPromises();
    expect(w.get('.step-note--error').text()).toMatch(/не загрузились/);
    await btn.trigger('click'); // свернуть
    await btn.trigger('click'); // раскрыть — повтор
    await flushPromises();
    expect(detailRequests(sent)).toHaveLength(2);
    expect(w.get('pre.step-pre').text()).toBe('ok');
    w.unmount();
  });

  it('событие без brief и подробностей рисуется как раньше — без кнопки', async () => {
    stubFetch();
    const w = mountWindow([ev({ id: 3, kind: 'tool_done', summary: 'готово (157 симв.)' })]);
    await flushPromises();
    const row = w.get('[data-event-id="3"]');
    expect(row.text()).toBe('готово (157 симв.)');
    expect(row.find('button').exists()).toBe(false);
    w.unmount();
  });

  it('конец хода: время, шаги и цена; у Claude Code — подсказка про условную цену', async () => {
    stubFetch();
    const w = mountWindow([
      ev({
        id: 4,
        kind: 'turn_end',
        summary: '12 с',
        brief: { type: 'usage', duration_ms: 12_000, steps: 5, cost_usd: 0.03, tokens: { input: 1, output: 2, cache_read: 3, cache_write: 4 } },
      }),
    ]);
    await flushPromises();
    const row = w.get('[data-event-id="4"]');
    expect(row.text()).toBe('ход окончен · 12 с · 5 шагов · $0.03');
    expect(row.get('.step-text').attributes('title')).toMatch(/по подписке деньги не списываются/);
    w.unmount();
  });
});

describe('Ф41-3а: строка сессии и «Усыпить»', () => {
  const usage = (tokens: number, limit: number | null, cost: number | null): SessionUsage => ({
    context_tokens: tokens,
    context_limit: limit,
    cost_usd: cost,
    updated_ts: NOW,
  });
  const cache = { last_ts: NOW, ttl_s: 3600, expires_ts: NOW + 58 * 60_000, context_tokens: 5000, model: 'sonnet' };

  it('«контекст 84 из 200 тыс. · кэш 58 мин · всего $1.20»; пороги цветом', () => {
    const w = mount(SessionCacheLine, { props: { cache, usage: usage(84_000, 200_000, 1.2), engine: 'claude', nowMs: NOW } });
    expect(w.text()).toBe('контекст 84 из 200 тыс. · кэш 58 мин · всего $1.20');
    expect(w.get('.cache-ctx').classes()).toContain('cache-ctx--normal');
    expect(w.get('.cache-cost').attributes('title')).toMatch(/условная цена/);
    const warn = mount(SessionCacheLine, { props: { usage: usage(150_000, 200_000, null), nowMs: NOW } });
    expect(warn.text()).toBe('контекст 150 из 200 тыс.');
    expect(warn.get('.cache-ctx').classes()).toContain('cache-ctx--warn');
    const danger = mount(SessionCacheLine, { props: { usage: usage(190_000, 200_000, null), nowMs: NOW } });
    expect(danger.get('.cache-ctx').classes()).toContain('cache-ctx--danger');
  });

  it('без usage — прежняя строка «кэш … · контекст …»', () => {
    const w = mount(SessionCacheLine, { props: { cache, usage: null, nowMs: NOW } });
    expect(w.text()).toBe('кэш 58 мин · контекст 5 тыс.');
  });

  it('блок сессий показывает строку, когда есть только usage (OpenCode)', async () => {
    const listed = session({ engine: 'opencode', usage: usage(30_000, 128_000, 0.5) });
    stubFetch({}, false, [listed]);
    store.state.projects = [];
    store.state.sessions.items[5] = listed;
    const w = mount(SessionsBlock, { props: { projectId: 3, nowMs: NOW } });
    await flushPromises();
    expect(w.get('.cache-line').text()).toBe('контекст 30 тыс. из 200 · всего $0.50');
    w.unmount();
  });

  it('«Усыпить» неактивна у спящей с пустой очередью; с репликой в очереди и у idle — активна', async () => {
    stubFetch();
    const asleep = mountWindow([], { state: 'asleep', queued_says: 0 });
    expect(asleep.get('.session-sleep').attributes('disabled')).toBeDefined();
    asleep.unmount();
    const queued = mountWindow([], { state: 'asleep', queued_says: 1 });
    expect(queued.get('.session-sleep').attributes('disabled')).toBeUndefined();
    queued.unmount();
    const idle = mountWindow([], { state: 'idle' });
    expect(idle.get('.session-sleep').attributes('disabled')).toBeUndefined();
    idle.unmount();
    await flushPromises();
  });
});
