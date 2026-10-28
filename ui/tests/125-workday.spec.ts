// Ф64-1 (`docs/architecture.md` и 10): рабочий день проекта в интерфейсе — галочка
// «Проект по времени», кнопки «Закончить день» / «Начать день» с подтверждением, метка «день закрыт с ЧЧ:ММ»,
// скрытие «Ждёт тебя» закрытых проектов с тихой строкой «ждёт N», «спит: день закрыт» у сессии.
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { HubSession, OutboxListItem, Project } from '../src/api/types';
import ClosedDayLines from '../src/components/ClosedDayLines.vue';
import DayControl from '../src/components/DayControl.vue';
import MappingPanel from '../src/components/MappingPanel.vue';
import { store } from '../src/store/instance';
import { dayNote } from '../src/util/agentState';
import { buildWaiting, closedDayLines, closedDayText, homeDecisionCount } from '../src/util/waiting';
import { waitSummary } from '../src/util/boardList';
import { dayClosedLabel } from '../src/util/workday';

enableAutoUnmount(afterEach);

const CLOSED_TS = new Date(2026, 9, 15, 19, 30, 0).getTime();

function project(over: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, ...over };
}

function draft(id: number, projectId: number): OutboxListItem {
  return {
    id, channel_id: id, project_id: projectId, channel_title: 'Беседа', text: 'Да', status: 'pending_approval',
    origin: 'api', error: null, attempts: 0, created_ts: id, sent_ts: null, ext_id: null,
  } as OutboxListItem;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function fakeFetch(opts: { projects: () => Project[]; dayStatus?: number; dayDetail?: string }) {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = { method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined };
    sent.push(s);
    if (s.path === '/api/v1/projects' && s.method === 'GET') return jsonResponse(opts.projects());
    if (s.path.endsWith('/day')) {
      if (opts.dayStatus && opts.dayStatus !== 200) return jsonResponse({ detail: opts.dayDetail }, opts.dayStatus);
      return jsonResponse({ open: (s.body as { open: boolean }).open, ts: CLOSED_TS });
    }
    if (s.method === 'GET') return jsonResponse([]);
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф64-1: галочка «Проект по времени»', () => {
  it('показывает состояние и шлёт PATCH {workday}', async () => {
    let current = [project({ workday: false, day_open: true })];
    store.state.projects = current;
    const { sent, fetchMock } = fakeFetch({ projects: () => current });
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const box = wrapper.get('.mapping-project-workday-box');
    expect((box.element as HTMLInputElement).checked).toBe(false);
    expect(wrapper.get('.mapping-project-workday').text()).toContain('Проект по времени');

    current = [project({ workday: true, day_open: true })];
    await box.setValue(true);
    await flushPromises();
    expect(sent.filter((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/3').map((p) => p.body)).toEqual([{ workday: true }]);
    expect((wrapper.get('.mapping-project-workday-box').element as HTMLInputElement).checked).toBe(true);
  });
});

describe('Ф64-1: кнопки дня', () => {
  it('у проекта не «по времени» ничего нет', () => {
    store.state.projects = [project()];
    const wrapper = mount(DayControl, { props: { projectId: 3 } });
    expect(wrapper.find('[data-test="day-toggle"]').exists()).toBe(false);
  });

  it('«Закончить день»: без подтверждения ничего не уходит; с подтверждением — POST open=false и перечитанные проекты', async () => {
    let current = [project({ workday: true, day_open: true, day_ts: null })];
    store.state.projects = current;
    const { sent, fetchMock } = fakeFetch({ projects: () => current });
    vi.stubGlobal('fetch', fetchMock);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    const wrapper = mount(DayControl, { props: { projectId: 3 } });
    expect(wrapper.get('[data-test="day-toggle"]').text()).toBe('Закончить день');
    expect(wrapper.find('[data-test="day-label"]').exists()).toBe(false);

    await wrapper.get('[data-test="day-toggle"]').trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.path.endsWith('/day'))).toEqual([]);
    expect(confirm.mock.calls[0]![0]).toContain('доделают текущий ход и уснут');
    expect(confirm.mock.calls[0]![0]).toContain('«Ждёт тебя» по проекту скрыто');

    current = [project({ workday: true, day_open: false, day_ts: CLOSED_TS })];
    await wrapper.get('[data-test="day-toggle"]').trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.path.endsWith('/day')).map((s) => [s.path, s.body])).toEqual([['/api/v1/projects/3/day', { open: false }]]);
    // проекты перечитаны сразу: кнопка сменилась, появилась метка
    expect(sent.filter((s) => s.path === '/api/v1/projects' && s.method === 'GET').length).toBeGreaterThan(0);
    expect(wrapper.get('[data-test="day-toggle"]').text()).toBe('Начать день');
    expect(wrapper.get('[data-test="day-label"]').text()).toMatch(/^день закрыт с \d{2}:\d{2}$/);
  });

  it('«Начать день» — без подтверждения, POST open=true', async () => {
    let current = [project({ workday: true, day_open: false, day_ts: CLOSED_TS })];
    store.state.projects = current;
    const { sent, fetchMock } = fakeFetch({ projects: () => current });
    vi.stubGlobal('fetch', fetchMock);
    const confirm = vi.spyOn(window, 'confirm');
    const wrapper = mount(DayControl, { props: { projectId: 3 } });
    current = [project({ workday: true, day_open: true, day_ts: CLOSED_TS + 1 })];
    await wrapper.get('[data-test="day-toggle"]').trigger('click');
    await flushPromises();
    expect(confirm).not.toHaveBeenCalled();
    expect(sent.filter((s) => s.path.endsWith('/day')).map((s) => s.body)).toEqual([{ open: true }]);
    expect(wrapper.get('[data-test="day-toggle"]').text()).toBe('Закончить день');
  });

  it('409 workday_off показывается текстом ответа', async () => {
    store.state.projects = [project({ workday: true, day_open: true })];
    const { fetchMock } = fakeFetch({ projects: () => store.state.projects, dayStatus: 409, dayDetail: 'workday_off' });
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const wrapper = mount(DayControl, { props: { projectId: 3 } });
    await wrapper.get('[data-test="day-toggle"]').trigger('click');
    await flushPromises();
    expect(wrapper.get('[data-test="day-error"]').text()).toBe('workday_off');
  });
});

describe('Ф64-1: метка', () => {
  it('«день закрыт с ЧЧ:ММ» только при day_open=false у проекта «по времени»', () => {
    expect(dayClosedLabel(project({ workday: true, day_open: false, day_ts: CLOSED_TS }))).toMatch(/^день закрыт с \d{2}:\d{2}$/);
    expect(dayClosedLabel(project({ workday: true, day_open: false, day_ts: null }))).toBe('день закрыт');
    expect(dayClosedLabel(project({ workday: true, day_open: true, day_ts: CLOSED_TS }))).toBeNull();
    expect(dayClosedLabel(project())).toBeNull();
  });

  it('сессия проекта: «спит: день закрыт» по полю ядра и по проекту; бодрой — нет', () => {
    const base = { id: 1, project_id: 3, state: 'asleep' } as HubSession;
    expect(dayNote({ ...base, agent_state: { day_closed_ts: CLOSED_TS } as never }, [project()])).toBe('спит: день закрыт');
    expect(dayNote(base, [project({ workday: true, day_open: false })])).toBe('спит: день закрыт');
    expect(dayNote({ ...base, state: 'running' } as HubSession, [project({ workday: true, day_open: false })])).toBeNull();
    expect(dayNote(base, [project({ workday: true, day_open: true })])).toBeNull();
  });
});

describe('Ф64-1: «Ждёт тебя» закрытого проекта скрыт', () => {
  const projects = [
    project({ id: 3, name: 'Umbrella', workday: true, day_open: false, day_ts: CLOSED_TS }),
    project({ id: 1, name: 'Globex', workday: true, day_open: true }),
  ];
  const input = () => ({
    projects,
    home: { react: [], drafts: [draft(1, 3), draft(2, 3), draft(3, 1)] },
    
  });

  it('buildWaiting, счётчик и сводка не считают закрытый проект; строка говорит, сколько скрыто', () => {
    const items = buildWaiting(input());
    expect(items.map((i) => i.project.id)).toEqual([1]);
    expect(homeDecisionCount(input())).toBe(1);
    expect(waitSummary(items, []).total).toBe(1);
    const lines = closedDayLines(input());
    expect(lines.map(closedDayText)).toEqual(['Umbrella: день закрыт · ждёт 2']);
  });

  it('день открыт — всё на месте, строк нет', () => {
    const open = [project({ id: 3, workday: true, day_open: true })];
    const inp = { projects: open, home: { react: [], drafts: [draft(1, 3)] }, };
    expect(buildWaiting(inp)).toHaveLength(1);
    expect(closedDayLines(inp)).toEqual([]);
  });

  it('тихая строка в интерфейсе — без кнопок', () => {
    store.state.projects = projects;
    store.state.home = { ...store.state.home, drafts: [draft(1, 3), draft(2, 3)], react: [] };
    const wrapper = mount(ClosedDayLines);
    expect(wrapper.get('[data-test="closed-day-line"]').text()).toBe('Umbrella: день закрыт · ждёт 2');
    expect(wrapper.find('button').exists()).toBe(false);
  });
});
