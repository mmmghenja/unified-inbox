// Ф60-1C (`docs/architecture.md`): панель помощника на интервью в окне сессии хаба, форма
// «⋯ → Интервью…», кадры `event: interview` общего потока `/sessions/live` (`line`, `state`, `resync`).
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Interview, InterviewLine } from '../src/api/types';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import { store } from '../src/store/instance';
import { resetInterview } from '../src/store/interview';
import { createSessionsState } from '../src/store/sessions';
import { layout } from '../src/util/layout';

const NOW = new Date(2026, 9, 14, 12, 0, 0).getTime();

class FakeES {
  static all: FakeES[] = [];
  readonly listeners = new Map<string, ((ev: { data: string }) => void)[]>();
  closed = false;
  constructor(public readonly url: string) {
    FakeES.all.push(this);
  }
  addEventListener(type: string, fn: (ev: { data: string }) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  close(): void {
    this.closed = true;
  }
  emit(type: string, data: unknown): void {
    for (const fn of this.listeners.get(type) ?? []) fn({ data: JSON.stringify(data) });
  }
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'помощник', role_no: 1, engine: 'claude', model: 'sonnet', effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW - 1000, updated_ts: NOW - 500, closed_ts: null,
    close_reason: null, created_by: 'ui', seq: 10, last_event_id: 20, last_turn_ts: null, failure: null, wait_reason: null,
    pending_requests: 0, queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false,
    engine_session_id: null, workdir: '/p', cache: null, usage: null, ...overrides,
  } as HubSession;
}

function interview(overrides: Partial<Interview> = {}): Interview {
  return {
    id: 7, session_id: 5, project_id: 3, mode: 'live', them_lang: 'ru-RU', status: 'active', started_ts: Date.now() - 12 * 60000 - 5000,
    ended_ts: null, fed_line_id: 0, hint_ts: null, reason: null, warn: null, last_line_ts: null, lines: 0, queued_says: 0,
    wait_reason: null, ...overrides,
  };
}

function line(id: number, who: 'me' | 'them', text: string, startMs = NOW + id * 1000): InterviewLine {
  return { id, interview_id: 7, who, start_ms: startMs, end_ms: startMs + 900, text, created_ts: startMs };
}

interface Sent { method: string; path: string; body: Record<string, unknown> | undefined }
let sent: Sent[] = [];
const mounted: { unmount(): void }[] = [];
let current: { interview: Interview | null; lines: InterviewLine[]; listener_alive: boolean; listener_seen_ms: number | null };
let replies: Record<string, () => Response> = {};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  FakeES.all = [];
  sent = [];
  replies = {};
  current = { interview: null, lines: [], listener_alive: true, listener_seen_ms: NOW };
  layout.mode = 'full';
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sessions = createSessionsState();
  resetInterview();
  vi.stubGlobal('EventSource', FakeES);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      sent.push({ method, path: url.pathname + url.search, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const hit = replies[`${method} ${url.pathname}`];
      if (hit) return hit();
      if (url.pathname === '/api/v1/interviews/current') return json(current);
      if (/^\/api\/v1\/interviews\/\d+\/lines$/.test(url.pathname)) return json({ lines: [] });
      if (/\/events$/.test(url.pathname)) return json([]);
      if (/^\/api\/v1\/sessions\/\d+$/.test(url.pathname)) return json({ session: session(), requests: [], says: [] });
      return json({});
    }),
  );
});

afterEach(() => {
  for (const w of mounted.splice(0)) w.unmount();
  layout.mode = 'full';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function mountWindow(s: HubSession = session()) {
  store.state.sessions.items[s.id] = s;
  store.state.sessions.events[s.id] = [];
  const wrapper = mount(SessionWindow, { props: { session: s } });
  mounted.push(wrapper);
  await flushPromises();
  return wrapper;
}

function es(): FakeES {
  const open = FakeES.all.filter((e) => !e.closed);
  expect(open).toHaveLength(1);
  return open[0]!;
}

describe('панель интервью', () => {
  it('нет интервью — панели нет, запрос current один', async () => {
    const w = await mountWindow();
    expect(w.find('.interview-panel').exists()).toBe(false);
    expect(sent.filter((r) => r.path === '/api/v1/interviews/current')).toHaveLength(1);
  });

  it('строки двух цветов, время, минуты и режим; текст только как текст', async () => {
    current.interview = interview();
    current.lines = [line(1, 'them', 'расскажите о себе'), line(2, 'me', '<b>я</b> работаю')];
    const w = await mountWindow();
    expect(w.find('.interview-head').text()).toBe('Интервью · 12 мин · по ходу');
    const items = w.findAll('.interview-line');
    expect(items).toHaveLength(2);
    expect(items[0]!.classes()).toContain('interview-line--them');
    expect(items[0]!.text()).toContain('Собеседник:');
    expect(items[1]!.classes()).toContain('interview-line--me');
    expect(items[1]!.text()).toContain('Я:');
    expect(items[0]!.find('.interview-line-time').text()).toMatch(/^\d\d:\d\d:\d\d$/);
    expect(items[1]!.find('b').exists()).toBe(false);
    expect(items[1]!.text()).toContain('<b>я</b> работаю');
  });

  it('интервью другой сессии — в этом окне панели нет', async () => {
    current.interview = interview({ session_id: 9 });
    const w = await mountWindow();
    expect(w.find('.interview-panel').exists()).toBe(false);
  });

  it('режим «по кнопке» в заголовке', async () => {
    current.interview = interview({ mode: 'button' });
    const w = await mountWindow();
    expect(w.find('.interview-head').text()).toContain('по кнопке');
  });

  it('warn, reason и «подача ждёт»', async () => {
    current.interview = interview({ warn: 'эхо в микрофоне', reason: 'тишина 20 мин', status: 'paused', wait_reason: 'лимит подписки до 14:00' });
    const w = await mountWindow();
    expect(w.find('.interview-warn').text()).toBe('эхо в микрофоне');
    expect(w.find('.interview-reason').text()).toBe('Пауза: тишина 20 мин');
    expect(w.find('.interview-wait').text()).toBe('подача ждёт: лимит подписки до 14:00');
    expect(w.find('.interview-pause').text()).toBe('Продолжить');
  });

  it('служба записи не запущена — строка и команда', async () => {
    current.interview = interview();
    current.listener_alive = false;
    const w = await mountWindow();
    expect(w.find('.interview-listener').text()).toContain('служба записи не запущена');
    expect(w.find('.interview-command').text()).toBe('systemctl --user enable --now hub-listen.service');
  });

  it('«Подскажи»: queued — «уже запрошено», иначе «подсказка запрошена»', async () => {
    current.interview = interview();
    replies['POST /api/v1/interviews/7/hint'] = () => json({ interview: interview(), queued: true, says: [], lines: 0 });
    const w = await mountWindow();
    await w.find('.interview-hint').trigger('click');
    await flushPromises();
    expect(sent.some((r) => r.method === 'POST' && r.path === '/api/v1/interviews/7/hint')).toBe(true);
    expect(w.find('.interview-note').text()).toBe('уже запрошено');
    replies['POST /api/v1/interviews/7/hint'] = () => json({ interview: interview(), queued: false, says: [3], lines: 2 });
    await w.find('.interview-hint').trigger('click');
    await flushPromises();
    expect(w.find('.interview-note').text()).toBe('подсказка запрошена');
  });

  it('пауза и продолжить', async () => {
    current.interview = interview();
    replies['POST /api/v1/interviews/7/pause'] = () => json({ interview: interview({ status: 'paused' }) });
    replies['POST /api/v1/interviews/7/resume'] = () => json({ interview: interview() });
    const w = await mountWindow();
    expect(w.find('.interview-pause').text()).toBe('Пауза');
    await w.find('.interview-pause').trigger('click');
    await flushPromises();
    expect(w.find('.interview-pause').text()).toBe('Продолжить');
    await w.find('.interview-pause').trigger('click');
    await flushPromises();
    expect(sent.some((r) => r.path === '/api/v1/interviews/7/resume')).toBe(true);
    expect(w.find('.interview-pause').text()).toBe('Пауза');
  });

  it('«Закончить» просит подтверждение; «не заканчивать» ничего не шлёт', async () => {
    current.interview = interview();
    replies['POST /api/v1/interviews/7/end'] = () => json({ interview: interview({ status: 'ended' }), queued: false, says: [4], lines: 5 });
    const w = await mountWindow();
    await w.find('.interview-end').trigger('click');
    expect(w.find('.interview-end-confirm').exists()).toBe(true);
    expect(sent.some((r) => r.path.endsWith('/end'))).toBe(false);
    await w.find('.interview-end-no').trigger('click');
    expect(w.find('.interview-end-confirm').exists()).toBe(false);
    await w.find('.interview-end').trigger('click');
    await w.find('.interview-end-yes').trigger('click');
    await flushPromises();
    expect(sent.some((r) => r.method === 'POST' && r.path === '/api/v1/interviews/7/end')).toBe(true);
    expect(w.find('.interview-head').text()).toBe('Интервью окончено');
    expect(w.find('.interview-end').exists()).toBe(false);
  });

  it('ошибка ручки — видимой строкой', async () => {
    current.interview = interview();
    replies['POST /api/v1/interviews/7/hint'] = () => json({ detail: 'интервью на паузе' }, 409);
    const w = await mountWindow();
    await w.find('.interview-hint').trigger('click');
    await flushPromises();
    expect(w.find('.interview-error').text()).toBe('интервью на паузе');
  });

  it('телефон: панели нет', async () => {
    current.interview = interview();
    layout.mode = 'phone';
    const w = await mountWindow();
    expect(w.find('.interview-panel').exists()).toBe(false);
    expect(sent.some((r) => r.path === '/api/v1/interviews/current')).toBe(false);
  });
});

describe('кадры потока', () => {
  it('state открывает панель, line добавляет строку, чужое интервью игнорируется', async () => {
    const w = await mountWindow();
    expect(w.find('.interview-panel').exists()).toBe(false);
    es().emit('interview', { type: 'state', interview_id: 7, session_id: 5, interview: interview(), listener_seen_ms: NOW, listener_alive: true });
    await flushPromises();
    expect(w.find('.interview-panel').exists()).toBe(true);
    es().emit('interview', { type: 'line', interview_id: 7, session_id: 5, line: line(1, 'them', 'первая') });
    es().emit('interview', { type: 'line', interview_id: 7, session_id: 5, line: line(1, 'them', 'первая') });
    es().emit('interview', { type: 'line', interview_id: 99, session_id: 5, line: line(2, 'me', 'чужая') });
    await flushPromises();
    const items = w.findAll('.interview-line');
    expect(items).toHaveLength(1);
    expect(items[0]!.text()).toContain('первая');
  });

  it('state с listener_alive=false показывает «служба записи не запущена»', async () => {
    current.interview = interview();
    const w = await mountWindow();
    expect(w.find('.interview-listener').exists()).toBe(false);
    es().emit('interview', { type: 'state', interview_id: 7, session_id: 5, interview: interview(), listener_seen_ms: null, listener_alive: false });
    await flushPromises();
    expect(w.find('.interview-listener').exists()).toBe(true);
  });

  it('resync: current и lines?after= перечитываются', async () => {
    current.interview = interview();
    current.lines = [line(1, 'them', 'раз')];
    const w = await mountWindow();
    sent = [];
    current.lines = [line(1, 'them', 'раз'), line(2, 'me', 'два')];
    replies['GET /api/v1/interviews/7/lines'] = () => json({ lines: [line(3, 'them', 'три')] });
    es().emit('interview', { type: 'resync' });
    await flushPromises();
    expect(sent.some((r) => r.path === '/api/v1/interviews/current')).toBe(true);
    expect(sent.some((r) => r.path.startsWith('/api/v1/interviews/7/lines') && r.path.includes('after=1'))).toBe(true);
    expect(w.findAll('.interview-line').map((l) => l.find('.interview-line-text').text())).toEqual(['раз', 'два', 'три']);
  });

  it('второго соединения с потоком нет', async () => {
    await mountWindow();
    expect(FakeES.all.filter((e) => !e.closed)).toHaveLength(1);
    expect(FakeES.all[0]!.url).toContain('/sessions/live');
  });
});

describe('форма «Интервью…»', () => {
  async function openForm(w: Awaited<ReturnType<typeof mountWindow>>) {
    await w.find('.session-more-toggle').trigger('click');
    await w.find('.session-more-interview').trigger('click');
    await flushPromises();
  }

  it('начать: режим и язык уходят в POST /interviews', async () => {
    replies['POST /api/v1/interviews'] = () => json({ interview: interview({ mode: 'button', them_lang: 'en-US' }) });
    const w = await mountWindow();
    await openForm(w);
    const selects = w.findAll('.session-interview-form select');
    await selects[0]!.setValue('button');
    await selects[1]!.setValue('en-US');
    await w.find('form.session-interview-form').trigger('submit');
    await flushPromises();
    const post = sent.find((r) => r.method === 'POST' && r.path === '/api/v1/interviews');
    expect(post?.body).toEqual({ session_id: 5, mode: 'button', them_lang: 'en-US' });
    expect(w.find('.interview-panel').exists()).toBe(true);
    expect(w.find('form.session-interview-form').exists()).toBe(false);
  });

  it('409 «уже идёт интервью в сессии N» — строка отказа, потом ссылка', async () => {
    replies['POST /api/v1/interviews'] = () => {
      current.interview = interview({ session_id: 9 });
      return json({ detail: 'уже идёт интервью в сессии 9' }, 409);
    };
    const w = await mountWindow();
    await openForm(w);
    await w.find('form.session-interview-form').trigger('submit');
    await flushPromises();
    expect(w.find('.session-interview-elsewhere').text()).toContain('идёт в сессии');
    expect(w.find('.session-interview-link').text()).toBe('#9');
  });

  it('интервью в другой сессии — вместо формы ссылка; клик раскрывает её окно', async () => {
    current.interview = interview({ session_id: 9 });
    const w = await mountWindow();
    await openForm(w);
    expect(w.find('form.session-interview-form').exists()).toBe(false);
    await w.find('.session-interview-link').trigger('click');
    await flushPromises();
    expect(store.state.sessions.expanded[9]).toBe(true);
  });

  it('интервью в этой сессии — вместо формы подсказка про панель', async () => {
    current.interview = interview();
    const w = await mountWindow();
    await openForm(w);
    expect(w.find('form.session-interview-form').exists()).toBe(false);
    expect(w.find('.session-interview-here').exists()).toBe(true);
  });

  it('на телефоне пункта меню нет (ни в окне, ни в меню столбца)', async () => {
    layout.mode = 'phone';
    const s = session();
    store.state.sessions.items[s.id] = s;
    const menu = mount(SessionMenuItems, { props: { win: null, session: s } });
    expect(menu.find('.session-menu-interview').exists()).toBe(false);
    layout.mode = 'full';
    const menu2 = mount(SessionMenuItems, { props: { win: null, session: s } });
    expect(menu2.find('.session-menu-interview').exists()).toBe(true);
  });
});
