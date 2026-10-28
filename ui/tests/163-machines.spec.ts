// Ф77-1 D (`docs/architecture.md`): машина у роли — чип у линии доски и в «Сессиях» телефона,
// окно «⋯ → Машины» с переименованием на месте, строка «Выполнить на машине» над командой «Открыть в терминале».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, Machine, Project, ProjectSub } from '../src/api/types';
import BoardAgent from '../src/components/BoardAgent.vue';
import BoardSession from '../src/components/BoardSession.vue';
import MachineChip from '../src/components/MachineChip.vue';
import MachinesPanel from '../src/components/MachinesPanel.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import SessionWindow from '../src/components/SessionWindow.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createPeerMsgState } from '../src/store/peerMsgs';
import { createSessionsState } from '../src/store/sessions';
import { initLayout } from '../src/util/layout';
import { machineTitle, terminalMachineLine, versionDiffers } from '../src/util/machines';
import { fakeMedia } from './media';

vi.mock('../src/util/copy', () => ({ copyText: vi.fn(async () => true) }));

const NOW = new Date(2026, 9, 22, 12, 0, 0).getTime();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function machine(overrides: Partial<Machine> = {}): Machine {
  return {
    host: 'aaaaaaaaaaaaaaaa',
    name: 'Ноут',
    hostname: 'laptop',
    display: 'Ноут',
    route: 's1',
    core: false,
    online: true,
    last_ts: Date.now() - 2 * 60_000,
    version: 'abcdef12',
    ...overrides,
  };
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

function project(): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, subs: true };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  initLayout(fakeMedia(390).matchMedia);
  store.state.sessions = createSessionsState();
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project()];
  store.state.home = { ...store.state.home, react: [], drafts: [] };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/subs') return json({ items: store.state.peerMsgs.agentSubs, channels: [] });
      if (p === '/api/v1/sessions') return json({ items: Object.values(store.state.sessions.items) });
      if (/^\/api\/v1\/sessions\/\d+$/.test(p)) {
        const id = Number(p.split('/').pop());
        return json({ session: store.state.sessions.items[id] ?? session({ id }), requests: [], says: [] });
      }
      return json([]);
    }),
  );
});

afterEach(() => {
  store.state.phoneTab = 'wait';
  store.state.phoneSession = null;
  initLayout(null);
  resetBoard();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф77-1 D: чип машины', () => {
  it('MachineChip: текст — display, подсказка — hostname, путь словами, последний контакт; нет машины — ничего', () => {
    const w = mount(MachineChip, { props: { machine: machine() } });
    expect(w.get('.machine-chip').text()).toBe('Ноут');
    const title = w.get('.machine-chip').attributes('title') ?? '';
    expect(title).toContain('laptop');
    expect(title).toContain('через relay');
    expect(title).toContain('последний контакт');
    expect(mount(MachineChip, { props: { machine: null } }).find('.machine-chip').exists()).toBe(false);
    expect(mount(MachineChip).find('.machine-chip').exists()).toBe(false);
  });

  it('machineTitle: машина ядра называется словами, без hostname — без пустых кусков', () => {
    expect(machineTitle(machine({ hostname: null, route: 'core', core: true, last_ts: null }))).toBe('машина ядра');
    expect(machineTitle(machine({ hostname: null, route: 's1-socks', last_ts: null }))).toBe('через relay (socks)');
    expect(machineTitle(machine({ hostname: null, route: 'lan', last_ts: null }))).toBe('локальная сеть');
  });

  it('чип у столбца сессии хаба (в «Сведениях» меню) и у внешней линии на доске', async () => {
    store.state.sessions.items = { 5: session({ machine: machine({ display: 'Станция', core: true, route: 'core' }) }) };
    const s = mount(BoardSession, { props: { item: { kind: 'session', id: 5 }, session: store.state.sessions.items[5]! } });
    await s.get('.board-col-menu').trigger('click');
    expect(s.get('.session-menu-info .machine-chip').text()).toBe('Станция');
    s.unmount();
    const line = {
      key: 's40',
      projectId: 3,
      name: 'маркетолог',
      hasRole: true,
      address: { project_id: 3, role: 'маркетолог', number: 1 },
      sub: sub({ machine: machine({ display: 'Ноут' }) }),
      cwd: null,
      unread: 0,
    };
    const a = mount(BoardAgent, { props: { item: { kind: 'agent', key: 's40' }, line } });
    expect(a.get('.machine-chip').text()).toBe('Ноут');
    a.unmount();
  });

  it('чип в «Сессиях» телефона: у сессии хаба и у внешней', async () => {
    store.state.phoneTab = 'orch';
    store.state.sessions.items = { 5: session({ machine: machine({ display: 'Станция' }) }) };
    store.state.peerMsgs.agentSubs = [sub({ machine: machine({ display: 'Ноут' }) })];
    const w = mount(PhoneShell);
    await flushPromises();
    expect(w.get('[data-session-id="5"] .machine-chip').text()).toBe('Станция');
    expect(w.get('[data-agent-key] .machine-chip').text()).toBe('Ноут');
    w.unmount();
  });
});

describe('Ф77-1 D: «Открыть в терминале»', () => {
  it('над командой — «Выполнить на машине «<display>»», команда без ssh', async () => {
    const s = session({ state: 'asleep', engine_session_id: 'abc-1', machine: machine({ display: 'Станция' }) });
    store.state.sessions.items[s.id] = s;
    const w = mount(SessionWindow, { props: { session: s } });
    await w.get('.session-terminal').trigger('click');
    await flushPromises();
    expect(w.get('[data-test="terminal-machine"]').text()).toBe('Выполнить на машине «Станция»');
    expect(w.get('.work-terminal-command').text()).toBe('cd /p && claude --resume abc-1');
    w.unmount();
  });

  it('машина неизвестна — строки нет', async () => {
    const s = session({ state: 'asleep', engine_session_id: 'abc-1' });
    store.state.sessions.items[s.id] = s;
    const w = mount(SessionWindow, { props: { session: s } });
    await w.get('.session-terminal').trigger('click');
    await flushPromises();
    expect(w.find('[data-test="terminal-machine"]').exists()).toBe(false);
    expect(terminalMachineLine(null)).toBeNull();
    w.unmount();
  });
});

describe('Ф77-1 D: окно «Машины»', () => {
  const core = machine({ host: 'cccccccccccccccc', name: null, hostname: 'desktop', display: 'desktop', route: 'core', core: true, version: 'abcdef12' });
  const laptop = machine({ version: '11111111' });
  const gone = machine({ host: 'bbbbbbbbbbbbbbbb', name: null, hostname: null, display: 'машина bbbbbb', online: false, version: null });

  function stub(patch?: (host: string, body: { name: string }) => Response): ReturnType<typeof vi.fn> {
    const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/machines' && (init?.method ?? 'GET') === 'GET') return json({ items: [core, laptop, gone], core: core.host });
      if (p.startsWith('/api/v1/machines/') && init?.method === 'PATCH') {
        return patch!(decodeURIComponent(p.split('/').pop()!), JSON.parse(String(init.body)));
      }
      return json([]);
    });
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('таблица: имя, hostname, «машина ядра», путь, версия (чужая — жёлтым), «не на связи»', async () => {
    stub();
    const w = mount(MachinesPanel);
    await flushPromises();
    const rows = w.findAll('[data-test="machine-row"]');
    expect(rows).toHaveLength(3);
    expect(rows[0]!.text()).toContain('машина ядра');
    expect(rows[1]!.text()).toContain('laptop');
    expect(rows[1]!.text()).toContain('через relay');
    expect(rows[1]!.text()).toContain('2 мин назад');
    expect(rows[0]!.find('.machines-version--diff').exists()).toBe(false);
    expect(rows[1]!.find('.machines-version--diff').exists()).toBe(true);
    expect(rows[1]!.find('[data-test="machine-offline"]').exists()).toBe(false);
    expect(rows[2]!.find('[data-test="machine-offline"]').text()).toBe('не на связи');
    expect(versionDiffers(laptop, 'abcdef12')).toBe(true);
    w.unmount();
  });

  it('клик по имени → поле, Enter сохраняет PATCH и обновляет строку; пусто снимает имя', async () => {
    const fetchMock = stub((host, body) => json({ machine: { ...laptop, name: body.name || null, display: body.name || 'laptop' } }));
    const w = mount(MachinesPanel);
    await flushPromises();
    await w.findAll('[data-test="machine-name"]')[1]!.trigger('click');
    const input = w.get('[data-test="machine-name-input"]');
    expect((input.element as HTMLInputElement).value).toBe('Ноут');
    await input.setValue('Рабочий');
    await input.trigger('keydown', { key: 'Enter' });
    await flushPromises();
    const call = fetchMock.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'PATCH')!;
    expect(String(call[0])).toContain('/machines/aaaaaaaaaaaaaaaa');
    expect(JSON.parse(String((call[1] as RequestInit).body))).toEqual({ name: 'Рабочий' });
    expect(w.findAll('[data-test="machine-name"]')[1]!.text()).toBe('Рабочий');
    // пусто — снять имя
    await w.findAll('[data-test="machine-name"]')[1]!.trigger('click');
    await w.get('[data-test="machine-name-input"]').setValue('  ');
    await w.get('[data-test="machine-name-input"]').trigger('keydown', { key: 'Enter' });
    await flushPromises();
    const last = fetchMock.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'PATCH').pop()!;
    expect(JSON.parse(String((last[1] as RequestInit).body))).toEqual({ name: '' });
    expect(w.findAll('[data-test="machine-name"]')[1]!.text()).toBe('laptop');
    w.unmount();
  });

  it('Esc отменяет правку без запроса; 422 — текстом под полем, поле остаётся', async () => {
    const fetchMock = stub(() => json({ detail: 'name: 1–32 печатных символов' }, 422));
    const w = mount(MachinesPanel);
    await flushPromises();
    await w.findAll('[data-test="machine-name"]')[1]!.trigger('click');
    await w.get('[data-test="machine-name-input"]').trigger('keydown', { key: 'Escape' });
    expect(w.find('[data-test="machine-name-input"]').exists()).toBe(false);
    expect(fetchMock.mock.calls.some((c) => (c[1] as RequestInit | undefined)?.method === 'PATCH')).toBe(false);
    await w.findAll('[data-test="machine-name"]')[1]!.trigger('click');
    await w.get('[data-test="machine-name-input"]').setValue('x');
    await w.get('[data-test="machine-name-input"]').trigger('keydown', { key: 'Enter' });
    await flushPromises();
    expect(w.get('[data-test="machine-error"]').text()).toContain('1–32');
    expect(w.find('[data-test="machine-name-input"]').exists()).toBe(true);
    w.unmount();
  });
});
