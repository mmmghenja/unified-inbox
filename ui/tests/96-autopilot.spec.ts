// Ф41-5 (интерфейс, `docs/architecture.md` П10/П14, 12): режим «Ручной | Авто» в окне сессии,
// панель «Автопилот» (только чтение с телефона, три положения ячейки проекта, мета-выключатели с подтверждением,
// «Поставить все»), заголовок карточки «Автопилот придержал: …», строки ленты, подписи. Ручек нет (404) — без ошибок.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { AutopilotState, HubSession, Project, SessionRequest } from '../src/api/types';
import AutopilotPanel from '../src/components/AutopilotPanel.vue';
import BoardColumnShell from '../src/components/BoardColumnShell.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import TopMenu from '../src/components/TopMenu.vue';
import { autopilotUi, resetAutopilot } from '../src/store/autopilot';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';
import { groupSwitches, META_GROUP, notesOf } from '../src/util/autopilot';
import { stepView } from '../src/util/sessionSteps';

const NOW = new Date(2026, 9, 8, 15, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

let sent: Sent[] = [];
let autopilot: () => Response = () => json({ detail: 'not found' }, 404);

function stubFetch(): void {
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      sent.push({ method, path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      const p = url.pathname;
      if (p.startsWith('/api/v1/autopilot')) return autopilot();
      if (/^\/api\/v1\/sessions\/\d+$/.test(p) && method === 'PATCH') {
        const body = JSON.parse(String(init?.body));
        return json({ session: session({ mode: body.mode, seq: 99 }) });
      }
      if (/^\/api\/v1\/sessions\/\d+$/.test(p)) return json({ session: session(), requests: [], says: [] });
      if (/\/events$/.test(p)) return json([]);
      return json([]);
    }),
  );
}

function state(overrides: Partial<AutopilotState> = {}): AutopilotState {
  return {
    switches: [
      { key: 'push', group: 'Наружу: push, серверы, выкладка', title: 'git push', manual: 'ask', meta: false, note: null },
      { key: 'tg_leave', group: 'Вопросы и заявки', title: 'Выйти из группы', manual: 'ask', meta: false, note: null },
      { key: 'hub_dirs', group: 'Хаб и его службы', title: 'Папки хаба', manual: 'deny', meta: true, note: 'данные хаба' },
    ],
    global: { push: true, tg_leave: true, hub_dirs: true },
    projects: { '3': { push: false } },
    local: true,
    ...overrides,
  };
}

function project(): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true } as Project;
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
    auto_held: 3,
    auto_free: [],
    ...overrides,
  } as HubSession;
}

function request(overrides: Partial<SessionRequest> = {}): SessionRequest {
  return {
    id: 100,
    session_id: 5,
    project_id: 3,
    kind: 'permission',
    tool: 'Bash',
    input: { command: 'git push' },
    input_truncated: false,
    reason: null,
    status: 'pending',
    answer: null,
    created_ts: NOW,
    decided_ts: null,
    delivered: false,
    seq: 11,
    rule: 'push',
    rule_title: 'git push',
    ...overrides,
  } as SessionRequest;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetAutopilot();
  autopilot = () => json({ detail: 'not found' }, 404);
  stubFetch();
  store.state.sessions = createSessionsState();
  store.state.projects = [project()];
  store.state.syncError = null;
});

afterEach(() => {
  resetAutopilot();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф41-5: панель «Автопилот»', () => {
  it('группы: мета-выключатели — отдельной группой внизу; пометки у tg_leave', () => {
    const groups = groupSwitches(state().switches);
    expect(groups.map((g) => g.title)).toEqual(['Наружу: push, серверы, выкладка', 'Вопросы и заявки', META_GROUP]);
    expect(groups.at(-1)!.meta).toBe(true);
    expect(notesOf({ key: 'tg_leave', note: null })).toEqual(['необратимо для закрытых групп']);
    expect(notesOf({ key: 'hub_dirs', note: 'данные хаба' })).toEqual(['данные хаба']);
  });

  it('таблица: строки по группам, подпись «карточка» / «запрет», «Общее» и проект', async () => {
    autopilot = () => json(state());
    const w = mount(AutopilotPanel);
    await flushPromises();
    expect(w.find('[data-switch="push"] .autopilot-kind').text()).toBe('карточка');
    expect(w.find('[data-switch="hub_dirs"] .autopilot-kind').text()).toBe('запрет');
    expect(w.find('[data-switch="hub_dirs"] .autopilot-note').text()).toBe('данные хаба');
    expect((w.get('[data-switch="push"] .autopilot-global input').element as HTMLInputElement).checked).toBe(true);
    expect((w.get('[data-switch="push"] select').element as HTMLSelectElement).value).toBe('free');
    expect((w.get('[data-switch="tg_leave"] select').element as HTMLSelectElement).value).toBe('inherit');
    w.unmount();
  });

  it('три положения ячейки проекта шлют true / false / null', async () => {
    autopilot = () => json(state());
    const w = mount(AutopilotPanel);
    await flushPromises();
    const select = w.get('[data-switch="tg_leave"] select');
    for (const [value, hold] of [['hold', true], ['free', false], ['inherit', null]] as const) {
      sent.length = 0;
      await select.setValue(value);
      await flushPromises();
      const put = sent.find((s) => s.method === 'PUT');
      expect(put?.path).toBe('/api/v1/autopilot');
      expect(put?.body).toEqual({ scope: 3, key: 'tg_leave', hold });
    }
    w.unmount();
  });

  it('«Общее» шлёт scope global; «Поставить все» — POST /autopilot/hold-all', async () => {
    autopilot = () => json(state());
    const w = mount(AutopilotPanel);
    await flushPromises();
    sent.length = 0;
    await w.get('[data-switch="push"] .autopilot-global input').setValue(false);
    await flushPromises();
    expect(sent.find((s) => s.method === 'PUT')?.body).toEqual({ scope: 'global', key: 'push', hold: false });
    sent.length = 0;
    await w.get('.autopilot-hold-all').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'POST' && s.path === '/api/v1/autopilot/hold-all')).toBe(true);
    w.unmount();
  });

  it('«Снять все»: одно подтверждение со списком, PUT по каждому придержанному; «Вернуть все» — без вопроса', async () => {
    autopilot = () => json(state());
    const w = mount(AutopilotPanel);
    await flushPromises();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    sent.length = 0;
    await w.get('.autopilot-free-all').trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(String(confirm.mock.calls[0][0])).toContain('Папки хаба');
    expect(sent.some((s) => s.method === 'PUT')).toBe(false);
    confirm.mockReturnValue(true);
    await w.get('.autopilot-free-all').trigger('click');
    await flushPromises();
    const puts = sent.filter((s) => s.method === 'PUT').map((s) => s.body);
    expect(puts).toEqual([
      { scope: 'global', key: 'push', hold: false },
      { scope: 'global', key: 'tg_leave', hold: false },
      { scope: 'global', key: 'hub_dirs', hold: false },
    ]);
    w.unmount();
  });

  it('столбец проекта: «всё сама» — одно подтверждение со списком (и мета), PUT только этому проекту; «всё спрашивать» — без вопроса; и с телефона', async () => {
    // Слова владельца. В проекте 3 push уже «сама».
    autopilot = () => json(state({ local: false }));
    const w = mount(AutopilotPanel);
    await flushPromises();
    expect(w.get('.autopilot-free-all').text()).toBe('Всё делает сама — во всех проектах');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    sent.length = 0;
    const col = w.get('[data-project-col="3"]');
    await col.get('.autopilot-project-free').trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(1);
    const text = String(confirm.mock.calls[0][0]);
    expect(text).toContain('Umbrella');
    expect(text).toContain('Выйти из группы');
    expect(text).toContain('Папки хаба (мета');
    expect(text).not.toContain('git push');
    expect(sent.some((s) => s.method === 'PUT')).toBe(false);
    confirm.mockReturnValue(true);
    await col.get('.autopilot-project-free').trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.method === 'PUT').map((s) => s.body)).toEqual([
      { scope: 3, key: 'tg_leave', hold: false },
      { scope: 3, key: 'hub_dirs', hold: false },
    ]);
    confirm.mockClear();
    sent.length = 0;
    await col.get('.autopilot-project-hold').trigger('click');
    await flushPromises();
    expect(confirm).not.toHaveBeenCalled();
    expect(sent.filter((s) => s.method === 'PUT').map((s) => s.body)).toEqual([
      { scope: 3, key: 'push', hold: true },
      { scope: 3, key: 'tg_leave', hold: true },
      { scope: 3, key: 'hub_dirs', hold: true },
    ]);
    w.unmount();
  });

  it('«снять группу» / «вернуть группу» трогают только свою группу', async () => {
    autopilot = () => json(state({ global: { push: false, tg_leave: true, hub_dirs: true } }));
    const w = mount(AutopilotPanel);
    await flushPromises();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    sent.length = 0;
    await w.get('[data-group="Вопросы и заявки"] .autopilot-group-free').trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.method === 'PUT').map((s) => s.body)).toEqual([
      { scope: 'global', key: 'tg_leave', hold: false },
    ]);
    sent.length = 0;
    await w.get('[data-group="Наружу: push, серверы, выкладка"] .autopilot-group-return').trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.method === 'PUT').map((s) => s.body)).toEqual([
      { scope: 'global', key: 'push', hold: true },
    ]);
    w.unmount();
  });

  it('снятие мета-выключателя спрашивает подтверждение: отказ — запроса нет, согласие — уходит', async () => {
    autopilot = () => json(state());
    const w = mount(AutopilotPanel);
    await flushPromises();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    sent.length = 0;
    await w.get('[data-switch="hub_dirs"] .autopilot-global input').setValue(false);
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(sent.some((s) => s.method === 'PUT')).toBe(false);
    confirm.mockReturnValue(true);
    await w.get('[data-switch="hub_dirs"] .autopilot-global input').setValue(false);
    await flushPromises();
    expect(sent.find((s) => s.method === 'PUT')?.body).toEqual({ scope: 'global', key: 'hub_dirs', hold: false });
    // обычный выключатель — без вопроса
    confirm.mockClear();
    await w.get('[data-switch="push"] .autopilot-global input').setValue(false);
    await flushPromises();
    expect(confirm).not.toHaveBeenCalled();
    w.unmount();
  });

  it('local: false (телефон, слова владельца) — меняется всё, как с ноутбука; подсказки «с ноутбука» нет', async () => {
    autopilot = () => json(state({ local: false }));
    const w = mount(AutopilotPanel);
    await flushPromises();
    expect(w.find('.autopilot-remote-note').exists()).toBe(false);
    expect(w.text()).not.toContain('Переключается с ноутбука');
    expect(w.get('.autopilot-hold-all').attributes('disabled')).toBeUndefined();
    for (const el of w.findAll('.autopilot-table input, .autopilot-table select')) {
      expect(el.attributes('disabled')).toBeUndefined();
    }
    sent.length = 0;
    await w.get('[data-switch="push"] .autopilot-global input').setValue(false);
    await flushPromises();
    expect(sent.find((s) => s.method === 'PUT')?.body).toEqual({ scope: 'global', key: 'push', hold: false });
    await w.get('.autopilot-hold-all').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'POST' && s.path === '/api/v1/autopilot/hold-all')).toBe(true);
    w.unmount();
  });

  it('ручки нет (404): пояснение, ошибок в полосе нет', async () => {
    const w = mount(AutopilotPanel);
    await flushPromises();
    expect(w.get('.autopilot-empty').text()).toContain('не отдаёт');
    expect(autopilotUi.error).toBeNull();
    expect(store.state.syncError).toBeNull();
    w.unmount();
  });
});

describe('Ф41-5: окно сессии в авто', () => {
  it('включение авто: подтверждение «придержано N из M», «Настроить» открывает панель; PATCH mode', async () => {
    autopilot = () => json(state());
    store.state.sessions.items = { 5: session() };
    const w = mount(SessionWindow, { props: { session: session() } });
    await flushPromises();
    sent.length = 0;
    await w.get('.session-mode-auto').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'PATCH')).toBe(false);
    expect(w.get('.session-auto-confirm').text()).toContain('Сейчас придержано 3 из 3.');
    await w.get('.session-auto-settings').trigger('click');
    expect(autopilotUi.open).toBe(true);
    await w.get('.session-mode-auto').trigger('click');
    await flushPromises();
    await w.get('.session-auto-yes').trigger('click');
    await flushPromises();
    const patch = sent.find((s) => s.method === 'PATCH');
    expect(patch?.path).toBe('/api/v1/sessions/5');
    expect(patch?.body).toEqual({ mode: 'auto' });
    w.unmount();
  });

  it('авто при всех стоящих выключателях: подсказка «равен ручному»; при снятых — нет', async () => {
    autopilot = () => json(state());
    const w = mount(SessionWindow, { props: { session: session({ mode: 'auto', auto_held: 3 }) } });
    await flushPromises();
    expect(w.get('.session-auto-hint').text()).toContain('Авто сейчас равен ручному');
    await w.setProps({ session: session({ mode: 'auto', auto_held: 2 }) });
    expect(w.find('.session-auto-hint').exists()).toBe(false);
    w.unmount();
  });

  it('возврат в ручной — сразу, без вопроса', async () => {
    store.state.sessions.items = { 5: session({ mode: 'auto' }) };
    const w = mount(SessionWindow, { props: { session: session({ mode: 'auto' }) } });
    await flushPromises();
    sent.length = 0;
    await w.get('.session-mode-manual').trigger('click');
    await flushPromises();
    expect(sent.find((s) => s.method === 'PATCH')?.body).toEqual({ mode: 'manual' });
    expect(w.find('.session-auto-confirm').exists()).toBe(false);
    w.unmount();
  });

  it('карточка запроса: в авто — «Автопилот придержал: <rule_title>», в ручном — как прежде', async () => {
    store.state.sessions.pending = { 5: [request()] };
    store.state.sessions.items = { 5: session({ mode: 'auto' }) };
    let w = mount(SessionWindow, { props: { session: session({ mode: 'auto' }) } });
    await flushPromises();
    expect(w.get('.work-request-head').text()).toContain('Автопилот придержал: git push');
    w.unmount();
    store.state.sessions.items = { 5: session() };
    w = mount(SessionWindow, { props: { session: session() } });
    await flushPromises();
    expect(w.get('.work-request-head').text()).toContain('Просит: Bash');
    expect(w.get('.work-request-head').text()).not.toContain('Автопилот');
    w.unmount();
  });

  it('слово «авто» в шапке столбца берётся из mode', () => {
    const w = mount(BoardColumnShell, { props: { item: { kind: 'session', id: 5 }, title: 'x', auto: true } });
    expect(w.get('.board-col-auto').text()).toBe('авто');
    const m = mount(BoardColumnShell, { props: { item: { kind: 'session', id: 5 }, title: 'x', auto: false } });
    expect(m.find('.board-col-auto').exists()).toBe(false);
  });

  it('строки ленты: mode, auto_answer, autopilot', () => {
    const ev = (brief: Record<string, unknown>) =>
      stepView({ id: 1, ts: NOW, kind: 'status', tool: null, summary: null, is_error: false, ref: null, brief } as never).text;
    expect(ev({ type: 'mode', mode: 'auto' })).toBe('режим: авто');
    expect(ev({ type: 'auto_answer', question: 'Какую ветку?' })).toBe('вопрос сессии — авто: решает сама · Какую ветку?');
    expect(ev({ type: 'autopilot', text: 'настройки автопилота изменились — применятся со следующего входа' })).toContain('применятся');
  });
});

describe('Ф41: панель «Автопилот» — отдельное окно', () => {
  it('смонтирована в body, а не внутри меню; Esc и клик по подложке закрывают', async () => {
    autopilot = () => json(state());
    const w = mount(TopMenu, { attachTo: document.body });
    autopilotUi.open = true;
    await flushPromises();
    const overlay = document.body.querySelector('.autopilot-overlay');
    expect(overlay).not.toBeNull();
    expect(w.element.contains(overlay)).toBe(false);
    expect(overlay!.parentElement).toBe(document.body);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(autopilotUi.open).toBe(false);
    expect(document.body.querySelector('.autopilot-overlay')).toBeNull();
    autopilotUi.open = true;
    await flushPromises();
    (document.body.querySelector('.autopilot-overlay') as HTMLElement).click();
    await flushPromises();
    expect(autopilotUi.open).toBe(false);
    w.unmount();
  });
});
