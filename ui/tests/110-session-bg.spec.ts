// Ф48-1: фоновые агенты сессии — чип, «ждёт агентов», окно со списком, подтверждение «Усыпить».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, SessionBgItem } from '../src/api/types';
import BoardSession from '../src/components/BoardSession.vue';
import SessionBgChip from '../src/components/SessionBgChip.vue';
import SessionBgModal from '../src/components/SessionBgModal.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { sessionRow } from '../src/util/boardList';
import { bgChipText, bgRows, bgSleepConfirm } from '../src/util/sessionBg';

const NOW = new Date(2026, 9, 9, 15, 0, 0).getTime();

function item(over: Partial<SessionBgItem>): SessionBgItem {
  return {
    id: 'a1', kind: 'agent', title: 'счёт', agent_type: 'worker', parent: null, state: 'running', started_ts: NOW - 5000,
    ended_ts: null, doing: null, last_tool: null, tool_uses: null, tokens: null, note: null, ...over,
  };
}

function session(bg: SessionBgItem[] | null | undefined, over: Partial<HubSession> = {}): HubSession {
  const s = {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: 'opus', effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
  if (bg !== undefined) s.bg = bg === null ? null : { items: bg };
  return s;
}

const cmd = (over: Partial<SessionBgItem> = {}): SessionBgItem => item({ id: 'c1', kind: 'command', agent_type: null, title: 'dev', ...over });

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  store.state.sessions = createSessionsState();
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ session: session(null), requests: [], says: [] }), { status: 200, headers: { 'content-type': 'application/json' } })));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

describe('чип', () => {
  it('тексты для сочетаний агентов, команд, законченных и null', () => {
    expect(bgChipText(session([item({}), item({ id: 'a2' })]))).toBe('агенты: 2');
    expect(bgChipText(session([cmd()]))).toBe('фон: 1');
    expect(bgChipText(session([item({}), cmd()]))).toBe('агенты: 1 · фон: 1');
    expect(bgChipText(session([item({ state: 'completed' }), cmd({ state: 'stopped' })]))).toBe('агенты: готово');
    expect(bgChipText(session([]))).toBeNull();
    expect(bgChipText(session(null))).toBeNull();
    expect(bgChipText(session(undefined))).toBeNull();
  });

  it('компонент: нет задач — нет чипа; законченные — серый; живые — зелёный', () => {
    expect(mount(SessionBgChip, { props: { session: session(null) } }).find('.session-bg-chip').exists()).toBe(false);
    const done = mount(SessionBgChip, { props: { session: session([item({ state: 'completed' })]) } });
    expect(done.get('.session-bg-chip').text()).toBe('агенты: готово');
    expect(done.find('.session-bg-chip--live').exists()).toBe(false);
    const live = mount(SessionBgChip, { props: { session: session([item({})]) } });
    expect(live.find('.session-bg-chip--live').exists()).toBe(true);
  });

  it('клик открывает окно', async () => {
    const w = mount(SessionBgChip, { props: { session: session([item({})]) }, attachTo: document.body });
    await w.get('.session-bg-chip').trigger('click');
    expect(document.body.querySelector('.session-bg-modal')).not.toBeNull();
    w.unmount();
  });
});

describe('состояние сессии', () => {
  it('idle с живыми задачами — «ждёт агентов», без них — «ждёт входа»', () => {
    expect(sessionRow(session([item({})]), undefined, NOW)).toMatchObject({ sub: 'ждёт агентов', dot: 'run' });
    expect(sessionRow(session([item({ state: 'completed' })]), undefined, NOW)).toMatchObject({ sub: 'ждёт входа · агенты: готово', dot: 'idle' });
    expect(sessionRow(session(null), undefined, NOW).sub).toBe('ждёт входа');
    expect(sessionRow(session([item({})], { state: 'running' }), undefined, NOW).sub).toBe('работает · агенты: 1');
  });

  it('столбец доски: точка состояния «ждёт агентов» и чип агентов в меню', async () => {
    const w = mount(BoardSession, {
      props: { item: { kind: 'session', id: 5 }, session: session([item({})]) },
      global: { stubs: { SessionWindow: true } },
    });
    expect(w.get('.board-col-dot').attributes('title')).toBe('ждёт агентов');
    expect(w.find('.board-dot--run').exists()).toBe(true);
    await w.get('.board-col-menu').trigger('click'); // Ф90-1: чип агентов — в «Сведениях» меню «⋯»
    expect(w.get('.session-menu-info .session-bg-chip').text()).toBe('агенты: 1');
  });
});

describe('окно со списком', () => {
  it('порядок: живые выше, новые выше; подагент под родителем; без родителя в списке — наверху', () => {
    const rows = bgRows([
      item({ id: 'old', state: 'completed', started_ts: NOW - 9000 }),
      item({ id: 'p', started_ts: NOW - 8000 }),
      item({ id: 'k1', kind: 'command', parent: 'p', started_ts: NOW - 7000 }),
      item({ id: 'new', state: 'completed', started_ts: NOW - 100 }),
      item({ id: 'orph', parent: 'missing', started_ts: NOW - 1000 }),
    ]);
    expect(rows.map((r) => [r.item.id, r.depth])).toEqual([['orph', 0], ['p', 0], ['k1', 1], ['new', 0], ['old', 0]]);
  });

  it('цикл родителей не теряет записи', () => {
    const rows = bgRows([item({ id: 'x', parent: 'y' }), item({ id: 'y', parent: 'x' })]);
    expect(rows.map((r) => r.item.id).sort()).toEqual(['x', 'y']);
  });

  it('строка: состояние, чем занят, инструмент, шаги, токены; живой счётчик идёт; у законченной — длительность', async () => {
    const w = mount(SessionBgModal, {
      props: {
        title: 'Агенты сессии программист',
        session: session([
          item({ id: 'a', title: 'ищет', doing: 'Running grep', last_tool: 'Bash', tool_uses: 3, tokens: 87382, started_ts: NOW - 65_000 }),
          item({ id: 'b', title: 'сделал', state: 'completed', started_ts: NOW - 100_000, ended_ts: NOW - 40_000, note: 'Итог\nпо делу' }),
          cmd({ parent: 'a', started_ts: NOW - 3000 }),
        ]),
      },
      attachTo: document.body,
    });
    const a = document.body.querySelector('[data-bg-id="a"]')!;
    expect(a.textContent).toContain('работает');
    expect(a.textContent).toContain('1 мин 05 с');
    expect(a.textContent).toContain('Running grep');
    expect(a.textContent).toContain('Bash');
    expect(a.textContent).toContain('шагов: 3');
    expect(a.textContent).toContain('токенов: 87 тыс.');
    vi.advanceTimersByTime(2000);
    await flushPromises();
    expect(document.body.querySelector('[data-bg-id="a"]')!.textContent).toContain('1 мин 07 с');
    const b = document.body.querySelector('[data-bg-id="b"]')!;
    expect(b.textContent).toContain('готово');
    expect(b.textContent).toContain('1 мин 00 с');
    expect(document.body.querySelector('[data-bg-id="c1"]')!.classList.contains('session-bg-item--child')).toBe(true);
    expect(document.body.querySelector('[data-bg-id="c1"]')!.textContent).toContain('команда');
    const note = b.querySelector('.session-bg-note') as HTMLElement;
    expect(note.classList.contains('session-bg-note--open')).toBe(false);
    note.click();
    await flushPromises();
    expect(document.body.querySelector('[data-bg-id="b"] .session-bg-note')!.classList.contains('session-bg-note--open')).toBe(true);
    w.unmount();
  });

  it('Esc закрывает', async () => {
    const w = mount(SessionBgModal, { props: { title: 't', session: session([item({})]) }, attachTo: document.body });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(w.emitted('close')).toHaveLength(1);
    w.unmount();
  });
});

describe('«Усыпить»', () => {
  it('текст называет числа; без живых задач подтверждения нет', () => {
    expect(bgSleepConfirm(session([item({}), item({ id: 'a2' }), cmd()]))).toBe('Работают агенты: 2, фоновых команд: 1 — они будут остановлены');
    expect(bgSleepConfirm(session([item({ state: 'completed' })]))).toBeNull();
    expect(bgSleepConfirm(session(null))).toBeNull();
  });

  it('кнопка спрашивает; «нет» — запроса нет, «да» — уходит', async () => {
    const w = mount(SessionWindow, { props: { session: session([item({}), cmd()]) } });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await w.get('.session-sleep').trigger('click');
    expect(confirm).toHaveBeenCalledWith('Работают агенты: 1, фоновых команд: 1 — они будут остановлены');
    const calls = (): number => (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.filter((c) => String(c[0]).includes('/sleep')).length;
    expect(calls()).toBe(0);
    confirm.mockReturnValue(true);
    await w.get('.session-sleep').trigger('click');
    await flushPromises();
    expect(calls()).toBe(1);
  });
});
