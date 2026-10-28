// Ф73-1 (docs/architecture.md): «Перечитать MCP» в меню «⋯» сессии хаба на всех движках —
// перезапуск процесса с продолжением разговора; пока просьба не исполнена — пункт занят, строка в состоянии.
import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { HubSession } from '../src/api/types';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import type { SessionWindowHandle } from '../src/util/sessionHandle';
import { sessionStatusParts } from '../src/util/sessionStatus';

const NOW = new Date(2026, 9, 19, 12, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: null, effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

function handle(): SessionWindowHandle {
  return {
    busy: false, canTerminal: false, nothingToSleep: false,
    sleep: vi.fn(), wake: vi.fn(), compact: vi.fn(), mcpReload: vi.fn(async () => {}), returnMain: vi.fn(async () => {}), close: vi.fn(),
    openPanel: vi.fn(), openRoleText: vi.fn(), openCompact: vi.fn(), openAlways: vi.fn(), openTerminal: vi.fn(), toggleSubEdit: vi.fn(), chooseMode: vi.fn(async () => {}), isAuto: false,
  };
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('Ф73-1: «Перечитать MCP»', () => {
  it('пункт есть у всех трёх движков и у спящей; клик зовёт окно', async () => {
    for (const engine of ['claude', 'opencode', 'cursor'] as const) {
      for (const state of ['idle', 'asleep', 'running'] as const) {
        const win = handle();
        const w = mount(SessionMenuItems, { props: { win, session: session({ engine, state }) } });
        const item = w.find('.session-menu-mcp-reload');
        expect(item.exists()).toBe(true);
        expect(item.text()).toBe('Перечитать MCP');
        await item.trigger('click');
        expect(win.mcpReload).toHaveBeenCalledTimes(1);
      }
    }
  });

  it('просьба не исполнена — пункт занят; закрытая сессия — недоступен', () => {
    const asked = mount(SessionMenuItems, { props: { win: handle(), session: session({ mcp_reload_ts: NOW }) } });
    const item = asked.find('.session-menu-mcp-reload');
    expect(item.text()).toBe('перечитываю MCP…');
    expect(item.attributes('disabled')).toBeDefined();
    const closed = mount(SessionMenuItems, { props: { win: handle(), session: session({ closed_ts: NOW }) } });
    expect(closed.find('.session-menu-mcp-reload').attributes('disabled')).toBeDefined();
  });

  it('строка состояния: во время хода — «после хода», иначе — «перечитываю»', () => {
    expect(sessionStatusParts(session({ state: 'running', mcp_reload_ts: NOW }), NOW)).toContain('перечитаю MCP после хода');
    expect(sessionStatusParts(session({ state: 'idle', mcp_reload_ts: NOW }), NOW)).toContain('перечитываю MCP…');
    expect(sessionStatusParts(session(), NOW).join(' ')).not.toContain('MCP');
  });
});
