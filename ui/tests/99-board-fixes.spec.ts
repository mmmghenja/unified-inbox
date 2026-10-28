// Ф41-6: правки после проверки в браузере — низ окна сессии и меню «⋯», строка состояния, «…» вместо номера беседы,
// 404 не опрашиваем повторно.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Project, ProjectSub } from '../src/api/types';
import BoardColumns from '../src/components/BoardColumns.vue';
import WorkEventFeed from '../src/components/WorkEventFeed.vue';
import BoardChannel from '../src/components/BoardChannel.vue';
import { boardState, loadOfflineLines, openColumn, resetBoard } from '../src/store/board';
import { sessionStatusParts } from '../src/util/sessionStatus';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let layoutAnswer: () => Response = () => json({ detail: 'not found' }, 404);

function stubFetch(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/ui/layout') return layoutAnswer();
      if (p === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      if (p === '/api/v1/sessions') return json({ items: Object.values(store.state.sessions.items) });
      if (p === '/api/v1/subs') return json({ items: store.state.peerMsgs.agentSubs, channels: [] });
      if (/\/events$/.test(p)) return json([]);
      if (/^\/api\/v1\/sessions\/\d+$/.test(p)) {
        const id = Number(p.split('/').pop());
        const found = store.state.sessions.items[id] ?? session({ id });
        const requests =
          found.pending_requests > 0
            ? [{ id: 100, session_id: id, project_id: found.project_id, kind: 'permission', tool: 'Bash', input: {}, input_truncated: false, reason: null, status: 'pending', answer: null, created_ts: NOW, decided_ts: null, delivered: false, seq: 11 }]
            : [];
        return json({ session: found, requests, says: [] });
      }
      return json([]);
    }),
  );
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true, ...overrides };
}

function session(overrides: Partial<HubSession> = {}): HubSession {
  return {
    id: 5,
    project_id: 3,
    role: 'программист',
    role_no: 1,
    engine: 'claude',
    model: null,
    effort: null,
    mode: 'manual',
    title: null,
    state: 'idle',
    host_seen_ts: NOW,
    created_ts: NOW,
    updated_ts: NOW,
    closed_ts: null,
    close_reason: null,
    created_by: 'ui',
    seq: 10,
    last_event_id: null,
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
    workdir: '/p',
    cache: null,
    usage: null,
    ...overrides,
  };
}

function sub(overrides: Partial<ProjectSub> = {}): ProjectSub {
  return {
    id: 40,
    project_id: 3,
    role: 'маркетолог',
    role_no: 1,
    mode: 'messages',
    engine: 'claude',
    session_name: null,
    live: true,
    listening: true,
    waiting: false,
    ...overrides,
  };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  layoutAnswer = () => json({ detail: 'not found' }, 404);
  stubFetch();
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project()];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
});

afterEach(() => {
  resetBoard();
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});


describe('Ф41-6: низ окна сессии в столбце', () => {
  async function mountColumn(overrides: Partial<HubSession> = {}) {
    store.state.sessions.items = { 5: session(overrides) };
    openColumn({ kind: 'session', id: 5 });
    const w = mount(BoardColumns, { attachTo: document.body });
    await flushPromises();
    return w;
  }

  it('простой: «Отправить» и «Файл»; «Стоп» и «Прервать и отправить» нет; нижних «⋯» и кнопок действий нет', async () => {
    const w = await mountColumn();
    expect(w.find('.session-stop').exists()).toBe(false);
    expect(w.find('.work-say-now').exists()).toBe(false);
    expect(w.find('.session-more').exists()).toBe(false);
    expect(w.find('.work-controls').exists()).toBe(false);
    // Ф90-1: низ — одна строка [📎] [поле] [🎤] [➤]: иконки с подсказками, без текстовых кнопок
    expect(w.get('.work-say-next').attributes('aria-label')).toBe('Отправить');
    expect(w.get('.session-attach').attributes('title')).toBe('Приложить файл');
    expect(w.find('.work-say-more').exists()).toBe(false);
    w.unmount();
  });

  it('идёт ход: «Стоп» и «Прервать и отправить» есть', async () => {
    const w = await mountColumn({ state: 'running' });
    expect(w.get('.session-stop').attributes('aria-label')).toBe('Стоп');
    // «Прервать и отправить» — в меню у ➤ (Ф90-1), не отдельной кнопкой в строке
    expect(w.find('.work-say-now').exists()).toBe(false);
    await w.get('.work-say-more').trigger('click');
    expect(w.get('.work-say-now').text()).toBe('Прервать и отправить');
    w.unmount();
  });

  it('меню «⋯» шапки: после «Левее/Правее» — действия сессии', async () => {
    const w = await mountColumn();
    await w.get('.board-col-menu').trigger('click');
    const text = w.get('.board-col-popup').text();
    for (const item of ['Левее', 'Правее', 'Усыпить', 'Сжать', 'Модель и усилие…', 'Новая с передачей дел…', 'Подписка…', 'Режим: ручной', 'Закрыть']) {
      expect(text).toContain(item);
    }
    expect(text.indexOf('Правее')).toBeLessThan(text.indexOf('Усыпить'));
    w.unmount();
  });

  it('строка состояния: модель · кэш · контекст · $; чего нет — не показывается', () => {
    const now = 1_000_000;
    const full = session({
      model: 'opus',
      cache: { last_ts: now, ttl_s: 300, expires_ts: now + 125_000, context_tokens: 0, model: 'opus' },
      usage: { context_tokens: 84_000, context_limit: 200_000, cost_usd: 1.5, updated_ts: now },
    });
    const parts = sessionStatusParts(full, now);
    expect(parts.slice(0, 3)).toEqual(['opus', 'кэш 02:05', 'контекст 84 тыс. из 200']);
    expect(parts).toHaveLength(4);
    expect(parts[3]).toBe('$1.50');
    expect(sessionStatusParts(session(), now)).toEqual([]);
  });
});

describe('Ф41-6: беседа-столбец и ручки, которых нет', () => {
  it('до ответа запись беседы — «…», а не номер; запись подгружается ручкой', async () => {
    store.state.sync.channelsById = {};
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        const p = new URL(input, 'http://localhost').pathname;
        calls.push(p);
        if (p === '/api/v1/channels/283') return json({ id: 283, project_id: 3, title: 'Беседа Х', kind: 'group', unread: 0, source: 'tg' });
        return json([]);
      }),
    );
    const w = mount(BoardChannel, { props: { item: { kind: 'channel', id: 283 }, channelId: 283 }, attachTo: document.body });
    expect(w.get('.board-col-title').text()).toContain('…');
    expect(w.text()).not.toContain('#283');
    await flushPromises();
    expect(calls).toContain('/api/v1/channels/283');
    expect(w.get('.board-col-title').text()).toContain('Беседа Х');
    w.unmount();
  });

  it('после 404 на /subs?offline=1 повторно не опрашиваем', async () => {
    let n = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        if (new URL(input, 'http://localhost').searchParams.get('offline') === '1') n += 1;
        return json({ detail: 'not found' }, 404);
      }),
    );
    await loadOfflineLines();
    await loadOfflineLines();
    expect(n).toBe(1);
  });

  it('после 404 на /agent-requests повторно не опрашиваем', async () => {
    let n = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        n += 1;
        return json({ detail: 'not found' }, 404);
      }),
    );
    store.state.agentRequests.gone = false;
    await store.loadAgentRequests();
    await store.loadAgentRequests();
    expect(n).toBe(1);
    store.state.agentRequests.gone = false;
  });
});

describe('Ф41-6: мелкие правки после второй проверки', () => {
  it('после сжатия (usage.context_tokens = null) — «контекст уточнится после хода», число кэша не берётся', () => {
    const now = 1_000_000;
    const s = session({
      cache: { last_ts: now, ttl_s: 300, expires_ts: now + 60_000, context_tokens: 34_000, model: 'opus' },
      usage: { context_tokens: null, context_limit: 200_000, cost_usd: null, updated_ts: now },
    });
    const parts = sessionStatusParts(s, now);
    expect(parts).toContain('контекст уточнится после хода');
    expect(parts.join(' ')).not.toContain('34 тыс');
  });
});

describe('Ф41: передача дел в ленте', () => {
  it('первая реплика «Передача дел.» — одной строкой, раскрывается по клику; обычная реплика целиком', async () => {
    const ev = (id: number, summary: string) => ({ id, kind: 'say', ts: 1, summary, tool: null, is_error: false }) as never;
    const w = mount(WorkEventFeed, {
      props: { events: [ev(1, 'Владелец (next): Передача дел. Ты продолжаешь работу прежней сессии…'), ev(2, 'владелец (next): привет')], olderDone: true, load: async () => undefined },
    });
    await flushPromises();
    const items = w.findAll('.work-event');
    expect(items[0]!.text()).toContain('передача дел от прежней сессии');
    expect(items[0]!.text()).not.toContain('Ты продолжаешь');
    expect(items[1]!.text()).toContain('ты написал: привет');
    await items[0]!.get('.handoff-say-toggle').trigger('click');
    expect(w.findAll('.work-event')[0]!.text()).toContain('Ты продолжаешь работу');
  });
});
