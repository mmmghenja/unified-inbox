// Ф83-3 (`docs/architecture.md`): запасные движки в интерфейсе — строка «Запасной» окна
// «Движок» (агент и факты, пометка «на запасном», «Вернуть на основной»), «Запасной» в форме «Модель и усилие»,
// умолчание проекта, чип «на запасном», «⋯ → Вернуть на основной» (409 текстом ядра), «Перейти на …» у паузы
// (окно сессии и карточка очереди).
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { EngineInfo, EngineName, EngineStatus, HubSession, Project, OwnerQueue } from '../src/api/types';
import AgentQueue from '../src/components/AgentQueue.vue';
import EnginePanel from '../src/components/EnginePanel.vue';
import ProjectFallbackRow from '../src/components/ProjectFallbackRow.vue';
import SessionFallbackChip from '../src/components/SessionFallbackChip.vue';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import SessionModelForm from '../src/components/SessionModelForm.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { createOwnerState } from '../src/store/owner_channel';
import {
  fallbackBody,
  hhmm,
  kindLabel,
  onFallbackLine,
  sessionFallbackDraft,
  sessionFallbackMode,
  sessionFallbackTitle,
  switchLabel,
} from '../src/util/engineFallback';
import type { SessionWindowHandle } from '../src/util/sessionHandle';

const NOW = new Date(2026, 9, 22, 15, 0, 0).getTime();
const SINCE = new Date(2026, 9, 22, 14, 5, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function info(name: EngineName, over: Partial<EngineInfo> = {}): EngineInfo {
  const base: Record<EngineName, EngineInfo> = {
    claude: {
      name: 'claude', label: 'Claude Code', available: true, reason: null,
      models: [{ id: 'sonnet', name: 'Sonnet', images: true, efforts: ['low', 'medium', 'high'] }],
      efforts: ['low', 'medium', 'high'], default_model: 'sonnet', default_effort: null, model: null, effort: null,
    },
    opencode: {
      name: 'opencode', label: 'OpenCode', available: true, reason: null,
      models: [{ id: 'deepseek/deepseek-flash', name: 'DeepSeek Flash', images: false, efforts: ['low', 'max'] }],
      efforts: ['low', 'max'], default_model: 'deepseek/deepseek-flash', default_effort: 'max', model: null, effort: null,
    },
    codex: {
      name: 'codex', label: 'Codex', available: false, reason: 'codex: нет входа', models: [], efforts: [],
      default_model: null, default_effort: null, model: null, effort: null,
    },
    cursor: {
      name: 'cursor', label: 'Cursor', available: true, reason: null,
      models: [{ id: 'grok-4.7', name: 'Grok 4.7', images: false, efforts: ['high'] }],
      efforts: ['high'], default_model: 'grok-4.7', default_effort: 'high', model: null, effort: null,
    },
  };
  return { ...base[name], ...over };
}

function engineStatus(over: Partial<EngineStatus> = {}): EngineStatus {
  return {
    engine: 'opencode', model: 'deepseek/deepseek-flash', effort: 'max', changed_ts: null,
    engines: [info('claude'), info('opencode'), info('codex'), info('cursor')],
    facts: { engine: 'opencode', model: 'deepseek/deepseek-flash', effort: 'max', follows_agent: true },
    agent_fallback: null, facts_fallback: null, fallback: null, on_fallback: null,
    effective: { engine: 'opencode', model: 'deepseek/deepseek-flash', effort: 'max' },
    ...over,
  };
}

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: 'sonnet', effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW - 1000, updated_ts: NOW - 500, closed_ts: null,
    close_reason: null, created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null,
    pending_requests: 0, queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false,
    engine_session_id: null, workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.projects = [];
  store.state.sessions = createSessionsState();
  store.state.owner_channel = createOwnerState();
  vi.stubGlobal('fetch', vi.fn(async () => json({})));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

// --- util ---------------------------------------------------------------------------------------------------

describe('Ф83-3: util/engineFallback', () => {
  it('пометка: «на запасном: … — причина с ЧЧ:ММ (основной: …)»', () => {
    const on = { main: { engine: 'opencode' as const, model: 'deepseek/deepseek-flash', effort: 'max' }, kind: 'limit', reason: 'x', since: SINCE };
    const line = onFallbackLine(on, { engine: 'claude', model: 'sonnet', effort: null });
    expect(line).toBe('на запасном: Claude Code · sonnet — лимит с 14:05 (основной: OpenCode · deepseek/deepseek-flash · max)');
    expect(hhmm(SINCE)).toBe('14:05');
  });

  it('настройка сессии: как у проекта / свой / нет и тело PATCH', () => {
    expect(sessionFallbackMode({ fallback: null })).toBe('inherit');
    expect(sessionFallbackMode({ fallback: { engine: null } })).toBe('none');
    const own = sessionFallbackDraft({ fallback: { engine: 'opencode', model: 'm', effort: 'low' } });
    expect(own).toEqual({ mode: 'own', engine: 'opencode', model: 'm', effort: 'low' });
    expect(fallbackBody({ ...own, model: ' ' })).toEqual({ engine: 'opencode', model: null, effort: 'low' });
    expect(fallbackBody({ mode: 'none', engine: '', model: '', effort: '' })).toEqual({ engine: null });
    expect(fallbackBody({ mode: 'inherit', engine: '', model: '', effort: '' })).toBeNull();
    expect(fallbackBody({ mode: 'own', engine: '', model: '', effort: '' })).toBeUndefined();
  });

  it('подсказка чипа — текст ядра; «Перейти на …» — по цели', () => {
    expect(sessionFallbackTitle({ on_fallback: null, on_fallback_text: 'текст ядра' })).toBe('текст ядра');
    expect(switchLabel({ fallback_target: { engine: 'opencode', model: 'deepseek/deepseek-flash', effort: null, source: 'project' } }))
      .toBe('Перейти на OpenCode · deepseek/deepseek-flash');
    expect(switchLabel({ fallback_target: null })).toBeNull();
  });
});

// --- окно «Движок» ------------------------------------------------------------------------------------------

describe('Ф83-3: окно «Движок» — строка «Запасной»', () => {
  async function mountPanel(status: EngineStatus) {
    vi.spyOn(api, 'getEngine').mockResolvedValue(status);
    vi.spyOn(store, 'refreshStatus').mockResolvedValue(undefined);
    const w = mount(EnginePanel, { attachTo: document.body });
    await flushPromises();
    return w;
  }

  it('у агента и фактов есть строка «Запасной»; у фактов движок основного недоступен с пояснением', async () => {
    const w = await mountPanel(engineStatus());
    expect(w.findAll('.engine-fallback')).toHaveLength(2);
    const facts = w.get('[data-fallback="facts"]');
    await facts.get('input[value="on"]').setValue(true);
    const main = facts.get('.engine-option[data-engine="opencode"]');
    expect(main.get('.engine-choose').attributes('disabled')).toBeDefined();
    expect(main.text()).toContain('это основной движок');
    expect(facts.get('.engine-option[data-engine="claude"] .engine-choose').attributes('disabled')).toBeUndefined();
    w.unmount();
  });

  it('Ф85-1: у агента запасной — и движок основного с другой моделью', async () => {
    const put = vi.spyOn(api, 'putEngineFallback').mockRejectedValue(
      new api.ApiError(422, 'engine: запасной совпадает с основным — нужна другая модель', null),
    );
    const w = await mountPanel(engineStatus());
    const agent = w.get('[data-block="agent"] [data-fallback="agent"]');
    await agent.get('input[value="on"]').setValue(true);
    const main = agent.get('.engine-option[data-engine="opencode"]');
    expect(main.get('.engine-choose').attributes('disabled')).toBeUndefined();
    expect(main.text()).not.toContain('это основной движок');
    await main.get('.engine-choose').trigger('click');
    await agent.get('.engine-apply-fallback').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith('agent', 'opencode', { model: 'deepseek/deepseek-flash', effort: 'max' });
    expect(w.get('.engine-notice').text()).toBe('engine: запасной совпадает с основным — нужна другая модель');
    expect(kindLabel('refusal')).toBe('модель отказалась');
    expect(kindLabel('transient')).toBe('временный сбой');
    w.unmount();
  });

  it('«задать» + «Применить запасной» — PUT /engine/fallback/agent; «нет» снимает', async () => {
    const put = vi.spyOn(api, 'putEngineFallback').mockImplementation(async (_who, engine) =>
      engineStatus({ agent_fallback: engine === null ? null : { engine, model: 'sonnet', effort: null } , fallback: engine === null ? null : { engine, model: 'sonnet', effort: null } }),
    );
    const w = await mountPanel(engineStatus());
    const row = w.get('[data-fallback="agent"]');
    await row.get('input[value="on"]').setValue(true);
    await row.get('.engine-option[data-engine="claude"] .engine-choose').trigger('click');
    await row.get('.engine-apply-fallback').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith('agent', 'claude', { model: 'sonnet', effort: null });
    expect(w.text()).toContain('Запасной агента: Claude Code · sonnet');
    await row.get('input[value="off"]').setValue(true);
    await row.get('.engine-apply-fallback').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenLastCalledWith('agent', null);
    w.unmount();
  });

  it('запасной фактов — свой вызов `facts`; 409 ядра показывается текстом', async () => {
    const put = vi.spyOn(api, 'putEngineFallback').mockRejectedValue(new api.ApiError(409, 'Claude Code недоступен', null));
    const w = await mountPanel(engineStatus());
    const row = w.get('[data-fallback="facts"]');
    await row.get('input[value="on"]').setValue(true);
    await row.get('.engine-option[data-engine="claude"] .engine-choose').trigger('click');
    await row.get('.engine-apply-fallback').trigger('click');
    await flushPromises();
    expect(put.mock.calls[0]![0]).toBe('facts');
    expect(w.get('.engine-notice').text()).toBe('Claude Code недоступен');
    w.unmount();
  });

  it('на запасном: в полях — основной, пометка и «Вернуть на основной»', async () => {
    const on = { main: { engine: 'opencode' as const, model: 'deepseek/deepseek-flash', effort: 'max' }, kind: 'balance', reason: 'пусто', since: SINCE };
    const back = vi.spyOn(api, 'returnEngineFallback').mockResolvedValue({ ...engineStatus(), returned: true });
    const w = await mountPanel(
      engineStatus({
        agent_fallback: { engine: 'claude', model: 'sonnet', effort: null },
        fallback: { engine: 'claude', model: 'sonnet', effort: null },
        on_fallback: on,
        effective: { engine: 'claude', model: 'sonnet', effort: null },
      }),
    );
    // основной выбор в полях — OpenCode, не действующий
    expect(w.get('[data-block="agent"] > .engine-choice .engine-option[data-engine="opencode"] .engine-choose').text()).toBe('OpenCode ✓');
    const note = w.get('[data-fallback="agent"] .engine-onfallback');
    expect(note.text()).toContain('на запасном: Claude Code · sonnet — кончился баланс с 14:05');
    await note.get('.engine-return').trigger('click');
    await flushPromises();
    expect(back).toHaveBeenCalledWith('agent');
    expect(w.text()).toContain('Вернул на основной.');
    expect(w.find('.engine-onfallback').exists()).toBe(false);
    w.unmount();
  });
});

// --- сессии -------------------------------------------------------------------------------------------------

describe('Ф83-3: сессия — форма, чип, меню, пауза', () => {
  it('«Модель и усилие»: «Запасной» — свой; свой движок сессии в списке нет; PATCH {fallback}', async () => {
    store.state.projects = [{ id: 3, session_fallback: { engine: 'opencode', model: 'deepseek/deepseek-flash', effort: null } } as Project];
    const patch = vi.spyOn(api, 'patchHubSession').mockResolvedValue({ session: session({ fallback: { engine: 'cursor', model: null, effort: null } }) } as never);
    vi.spyOn(api, 'getHubSession').mockResolvedValue({ session: session(), requests: [], says: [] } as never);
    const w = mount(SessionModelForm, { props: { session: session() } });
    expect(w.text()).toContain('у проекта: OpenCode · deepseek/deepseek-flash');
    await w.get('input[value="own"]').setValue(true);
    const options = w.findAll('.session-fallback-engine option').map((o) => o.text());
    expect(options).not.toContain('Claude Code');
    expect(options).toContain('Cursor');
    await w.get('.session-fallback-engine').setValue('cursor');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(patch).toHaveBeenCalledTimes(1); // модель не менялась — запрос только про запасной
    expect(patch).toHaveBeenCalledWith(5, { fallback: { engine: 'cursor', model: null, effort: null } });
    expect(w.emitted('done')).toBeTruthy();
  });

  it('«нет» — {engine: null}; «свой» без движка — ошибка формы без запроса', async () => {
    const patch = vi.spyOn(api, 'patchHubSession').mockResolvedValue({ session: session() } as never);
    vi.spyOn(api, 'getHubSession').mockResolvedValue({ session: session(), requests: [], says: [] } as never);
    const w = mount(SessionModelForm, { props: { session: session() } });
    await w.get('input[value="own"]').setValue(true);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.get('[role="alert"]').text()).toBe('Запасной: выбери движок.');
    expect(patch).not.toHaveBeenCalled();
    await w.get('input[value="none"]').setValue(true);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(patch).toHaveBeenCalledWith(5, { fallback: { engine: null } });
  });

  it('умолчание проекта: выбор движка — PATCH /projects/{id} {session_fallback}; «не задан» — null', async () => {
    const patch = vi.spyOn(api, 'patchProject').mockResolvedValue({ ok: true });
    const w = mount(ProjectFallbackRow, { props: { project: { id: 3, name: 'П' } as Project, busy: false } });
    await w.get('.mapping-project-fallback-engine').setValue('opencode');
    await flushPromises();
    expect(patch).toHaveBeenCalledWith(3, { session_fallback: { engine: 'opencode', model: null, effort: null } });
    expect(w.emitted('saved')).toBeTruthy();
    await w.get('.mapping-project-fallback-engine').setValue('');
    await flushPromises();
    expect(patch).toHaveBeenLastCalledWith(3, { session_fallback: null });
  });

  it('24.10: модель запасного — подсказки моделей выбранного движка (окно сессии и умолчание проекта)', async () => {
    const w = mount(SessionModelForm, { props: { session: session() } });
    await w.get('input[value="own"]').setValue(true);
    await w.get('.session-fallback-engine').setValue('opencode');
    const input = w.get('.session-fallback-model');
    const list = w.get(`datalist#${input.attributes('list')}`);
    expect(list.findAll('option').map((o) => o.attributes('value'))).toContain('anthropic/claude-sonnet-5-5');
    const row = mount(ProjectFallbackRow, { props: { project: { id: 3, name: 'П' } as Project, busy: false } });
    await row.get('.mapping-project-fallback-engine').setValue('opencode');
    await flushPromises();
    const rowInput = row.get('.mapping-project-fallback-model');
    const rowList = row.get(`datalist#${rowInput.attributes('list')}`);
    expect(rowList.findAll('option').map((o) => o.attributes('value'))).toContain('anthropic/claude-sonnet-5-5');
  });

  it('чип «на запасном» только при пометке; подсказка — текст ядра', () => {
    const off = mount(SessionFallbackChip, { props: { session: session() } });
    expect(off.find('.session-fallback-chip').exists()).toBe(false);
    const on = mount(SessionFallbackChip, {
      props: {
        session: session({
          on_fallback: { main: { engine: 'claude', model: 'opus', effort: null }, kind: 'limit', reason: 'x', since: SINCE, by: 'auto' },
          on_fallback_text: 'на запасном: OpenCode — лимит Claude Code с 14:05',
        }),
      },
    });
    expect(on.get('.session-fallback-chip').text()).toBe('на запасном');
    expect(on.get('.session-fallback-chip').attributes('title')).toBe('на запасном: OpenCode — лимит Claude Code с 14:05');
  });

  it('«⋯ → Вернуть на основной» — только на запасном; клик зовёт окно', async () => {
    const handle = { busy: false, returnMain: vi.fn(async () => {}) } as unknown as SessionWindowHandle;
    const plain = mount(SessionMenuItems, { props: { win: handle, session: session() } });
    expect(plain.find('.session-menu-fallback-return').exists()).toBe(false);
    const on = mount(SessionMenuItems, {
      props: {
        win: handle,
        session: session({ on_fallback: { main: { engine: 'claude', model: null, effort: null }, kind: 'limit', reason: null, since: SINCE, by: 'owner_channel' } }),
      },
    });
    await on.get('.session-menu-fallback-return').trigger('click');
    expect(handle.returnMain).toHaveBeenCalledTimes(1);
  });

  it('окно сессии: возврат с 409 — текст ядра в окне; пауза даёт «Перейти на …» → ответ fallback', async () => {
    const sent: Array<{ method: string; path: string; body: unknown }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        const url = new URL(input, 'http://localhost');
        sent.push({ method: init?.method ?? 'GET', path: decodeURIComponent(url.pathname), body: init?.body ? JSON.parse(String(init.body)) : undefined });
        if (url.pathname.endsWith('/fallback/return')) return json({ detail: 'Claude Code на паузе до 16:30' }, 409);
        if (/^\/api\/v1\/sessions\/\d+$/.test(url.pathname)) return json({ session: session(), requests: [], says: [] });
        if (url.pathname === '/api/v1/owner_channel/queue') return json({ items: [], counts: {}, loaded_at: NOW });
        if (url.pathname === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
        if (/\/events$/.test(url.pathname)) return json([]);
        return json({});
      }),
    );
    const w = mount(SessionWindow, {
      props: {
        session: session({
          wait_reason: 'paused',
          queued_says: 1,
          fallback_target: { engine: 'opencode', model: 'deepseek/deepseek-flash', effort: null, source: 'project' },
          on_fallback: { main: { engine: 'claude', model: null, effort: null }, kind: 'limit', reason: null, since: SINCE, by: 'owner_channel' },
        }),
      },
      attachTo: document.body,
    });
    await flushPromises();
    const exposed = w.vm as unknown as SessionWindowHandle;
    await exposed.returnMain();
    await flushPromises();
    expect(w.text()).toContain('Claude Code на паузе до 16:30');
    const btn = w.get('.session-fallback-switch');
    expect(btn.text()).toBe('Перейти на OpenCode · deepseek/deepseek-flash');
    await btn.trigger('click');
    await flushPromises();
    const answer = sent.find((s) => s.method === 'POST' && s.path.endsWith('/answer'));
    expect(answer?.path).toBe('/api/v1/owner_channel/paused:5/answer');
    expect(answer?.body).toEqual({ action: 'fallback' });
    w.unmount();
  });

  it('карточка очереди `paused`: ответ «Перейти на …» от ядра — кнопка рядом с «Отправить всё равно»', async () => {
    const fixture = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
    const base = fixture.items[0]!;
    const card = {
      ...base,
      ref: 'paused:5',
      kind: 'paused',
      full: { title: 'программист', pending: 1, queued_says: 1, fallback: { engine: 'opencode', model: 'm', effort: null, source: 'project' } },
      answers: [
        { action: 'wake', label: 'Отправить всё равно', needs_text: false },
        { action: 'fallback', label: 'Перейти на OpenCode · m', needs_text: false },
      ],
    };
    const sent: Array<{ path: string; body: unknown }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        const url = new URL(input, 'http://localhost');
        if (url.pathname === '/api/v1/owner_channel/queue') return json({ ...fixture, items: [card] });
        if (init?.method === 'POST' && decodeURIComponent(url.pathname).endsWith('/answer')) {
          sent.push({ path: decodeURIComponent(url.pathname), body: JSON.parse(String(init.body)) });
          return json({ ref: 'paused:5', status: 'switched' });
        }
        if (url.pathname === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
        return json({});
      }),
    );
    const w = mount(AgentQueue, { props: { nowMs: NOW }, attachTo: document.body });
    await flushPromises();
    const buttons = w.findAll('.owner-answer').map((b) => b.text());
    expect(buttons).toContain('Отправить всё равно');
    expect(buttons).toContain('Перейти на OpenCode · m');
    await w.findAll('.owner-answer').find((b) => b.text() === 'Перейти на OpenCode · m')!.trigger('click');
    await flushPromises();
    expect(sent).toEqual([{ path: '/api/v1/owner_channel/paused:5/answer', body: { action: 'fallback' } }]);
    w.unmount();
  });
});
