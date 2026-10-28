// Ф41-6a (`docs/architecture.md`): раскладка «Главной» — нормализация ответа ядра, действия
// (открыть/закрыть/сдвинуть столбец, число столбцов, список, панель «Ждёт», скрытые проекты), сохранение с задержкой
// 800 мс и работа в памяти, пока ручки `/ui/layout` нет (404) или нет сети.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import {
  boardState,
  closeColumn,
  defaultLayout,
  hideProject,
  itemKey,
  loadLayout,
  moveColumn,
  normalizeLayout,
  openColumn,
  pruneSessions,
  resetBoard,
  SAVE_DELAY_MS,
  setColumns,
  showProject,
  toggleList,
  toggleWait,
} from '../src/store/board';
import { store } from '../src/store/instance';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function stubFetch(handler: (method: string, path: string) => Response | Promise<Response>): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      sent.push({ method, path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return handler(method, url.pathname);
    }),
  );
  return sent;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
});

afterEach(() => {
  resetBoard();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-6a: умолчание и нормализация', () => {
  it('умолчание: список развёрнут, 3 столбца, открыто пусто, панель «Ждёт» открыта', () => {
    expect(defaultLayout()).toEqual({
      v: 1,
      list_collapsed: false,
      columns: 3,
      wait_open: true,
      open: [],
      hidden_projects: [],
      hidden_lines: [],
      view: 'own',
      view_project: null,
      picks: {},
    });
    expect(boardState.layout).toEqual(defaultLayout());
  });

  it('нормализация: лишнее выбрасывается, столбцов 1…5, списки до 40, дубли и мусор убираются', () => {
    const open = [
      { kind: 'session', id: 12 },
      { kind: 'session', id: 12 },
      { kind: 'agent', key: 'p3:маркетолог:1' },
      { kind: 'channel', id: 283 },
      { kind: 'orch' },
      { kind: 'bogus' },
      { kind: 'session' },
      { kind: 'agent', key: '' },
      'строка',
    ];
    const norm = normalizeLayout({ v: 7, list_collapsed: true, columns: 99, wait_open: false, open, hidden_projects: [4, 'x', 5.5], extra: 1 });
    expect(norm).toEqual({
      v: 1,
      list_collapsed: true,
      columns: 5,
      wait_open: false,
      open: [
        { kind: 'session', id: 12 },
        { kind: 'agent', key: 'p3:маркетолог:1' },
        { kind: 'channel', id: 283 },
        { kind: 'owner_channel' },
      ],
      hidden_projects: [4],
      hidden_lines: [],
      view: 'own',
      view_project: null,
      picks: {},
    });
    expect(normalizeLayout({ columns: 0 }).columns).toBe(1);
    expect(normalizeLayout('мусор')).toEqual(defaultLayout());
    const many = normalizeLayout({ open: Array.from({ length: 60 }, (_, i) => ({ kind: 'session', id: i })) });
    expect(many.open).toHaveLength(40);
  });
});

describe('Ф41-6a: действия раскладки', () => {
  it('openColumn: новый — справа; уже открытый — не дублируется, но подсвечивается (focusSeq растёт)', () => {
    openColumn({ kind: 'session', id: 1 });
    openColumn({ kind: 'owner_channel' });
    const seq = boardState.focusSeq;
    openColumn({ kind: 'session', id: 1 });
    expect(boardState.layout.open).toEqual([{ kind: 'session', id: 1 }, { kind: 'owner_channel' }]);
    expect(boardState.focusSeq).toBe(seq + 1);
    expect(boardState.focusKey).toBe(itemKey({ kind: 'session', id: 1 }));
  });

  it('closeColumn и moveColumn («Левее»/«Правее») меняют порядок, края не двигаются', () => {
    openColumn({ kind: 'session', id: 1 });
    openColumn({ kind: 'session', id: 2 });
    openColumn({ kind: 'owner_channel' });
    moveColumn({ kind: 'owner_channel' }, -1);
    expect(boardState.layout.open.map(itemKey)).toEqual(['session:1', 'owner_channel:', 'session:2']);
    moveColumn({ kind: 'session', id: 1 }, -1);
    moveColumn({ kind: 'session', id: 2 }, 1);
    expect(boardState.layout.open.map(itemKey)).toEqual(['session:1', 'owner_channel:', 'session:2']);
    closeColumn({ kind: 'owner_channel' });
    expect(boardState.layout.open.map(itemKey)).toEqual(['session:1', 'session:2']);
  });

  it('setColumns держит 1…5; toggleList; toggleWait; скрыть и показать проект', () => {
    setColumns(9);
    expect(boardState.layout.columns).toBe(5);
    setColumns(0);
    expect(boardState.layout.columns).toBe(1);
    toggleList();
    expect(boardState.layout.list_collapsed).toBe(true);
    toggleWait(null);
    expect(boardState.layout.wait_open).toBe(false);
    toggleWait(3);
    expect(boardState.layout.wait_open).toBe(true);
    expect(boardState.waitProject).toBe(3);
    toggleWait('all');
    expect(boardState.waitProject).toBeNull();
    hideProject(4);
    hideProject(4);
    expect(boardState.layout.hidden_projects).toEqual([4]);
    showProject(4);
    expect(boardState.layout.hidden_projects).toEqual([]);
  });

  it('pruneSessions убирает только столбцы закрытых сессий', () => {
    openColumn({ kind: 'session', id: 1 });
    openColumn({ kind: 'session', id: 2 });
    openColumn({ kind: 'agent', key: 'p3:роль:1' });
    pruneSessions(new Set([2]));
    expect(boardState.layout.open.map(itemKey)).toEqual(['session:2', 'agent:p3:роль:1']);
  });
});

describe('Ф41-6a: сохранение и работа в памяти', () => {
  it('раскладка без ответа ядра (404): правки живут в памяти, ошибок в полосе нет, PUT уходит один раз через 800 мс', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const sent = stubFetch(() => json({ detail: 'not found' }, 404));
    await loadLayout();
    expect(boardState.loaded).toBe(true);
    expect(boardState.layout).toEqual(defaultLayout());
    setColumns(4);
    openColumn({ kind: 'owner_channel' });
    toggleList();
    expect(boardState.layout).toMatchObject({ columns: 4, list_collapsed: true, open: [{ kind: 'owner_channel' }] });
    expect(sent.filter((s) => s.method === 'PUT')).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS - 1);
    expect(sent.filter((s) => s.method === 'PUT')).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    const puts = sent.filter((s) => s.method === 'PUT');
    expect(puts).toHaveLength(1);
    expect(puts[0]!.path).toBe('/api/v1/ui/layout');
    expect(puts[0]!.body).toEqual({ layout: expect.objectContaining({ columns: 4, list_collapsed: true }) });
    // после отказа ядра раскладка не потеряна и ошибок нет
    expect(boardState.layout.columns).toBe(4);
    expect(store.state.syncError).toBeNull();
    expect(store.state.sessions.error).toBeNull();
  });

  it('нет сети: загрузка и сохранение тихо проглатывают ошибку', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))));
    await expect(loadLayout()).resolves.toBeUndefined();
    setColumns(2);
    await vi.advanceTimersByTimeAsync(SAVE_DELAY_MS + 10);
    expect(boardState.layout.columns).toBe(2);
  });

  it('несколько правок подряд — одно сохранение после последней', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const sent = stubFetch(() => json({ layout: {} }));
    setColumns(2);
    await vi.advanceTimersByTimeAsync(500);
    setColumns(4);
    await vi.advanceTimersByTimeAsync(500);
    expect(sent.filter((s) => s.method === 'PUT')).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(400);
    expect(sent.filter((s) => s.method === 'PUT')).toHaveLength(1);
  });

  it('ответ ядра применяется (с нормализацией), но не затирает правку, сделанную до ответа', async () => {
    stubFetch(() => json({ layout: { v: 1, columns: 2, open: [{ kind: 'session', id: 7 }], list_collapsed: true } }));
    await loadLayout();
    expect(boardState.layout).toMatchObject({ columns: 2, list_collapsed: true, open: [{ kind: 'session', id: 7 }] });
    resetBoard();
    toggleWait(null);
    await loadLayout();
    expect(boardState.layout.columns).toBe(3); // ответ пришёл после правки — правка (dirty) осталась
    expect(boardState.layout.wait_open).toBe(false);
  });

  it('ядро вернуло null — остаётся умолчание', async () => {
    stubFetch(() => json({ layout: null }));
    await loadLayout();
    expect(boardState.layout).toEqual(defaultLayout());
  });
});
