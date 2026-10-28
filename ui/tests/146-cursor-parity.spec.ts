// Ф74-8 (docs/architecture.md): чего у Cursor нет — интерфейс говорит прямо, по профилю.
import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it } from 'vitest';

import type { HubSession, SessionEngineProfile, StatusResponse } from '../src/api/types';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import { store } from '../src/store/instance';
import { switchDeadOn } from '../src/util/autopilot';
import { DEFAULT_SESSION_ENGINES } from '../src/util/engineProfiles';
import { SAY_TOOL_ONLY_REASON, sayQueueReason } from '../src/util/sayQueue';

const NOW = new Date(2026, 9, 21, 12, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 7, project_id: 3, role: 'программист', role_no: 1, engine: 'cursor', model: null, effort: null, mode: 'auto',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

afterEach(() => {
  store.state.status = null;
  document.body.innerHTML = '';
});

describe('Ф74-8: паритет Cursor в интерфейсе', () => {
  it('у Cursor вместо «Сжать» — причина строкой; у Claude — пункты, без строки', () => {
    const cursor = mount(SessionMenuItems, { props: { win: null, session: session() } });
    expect(cursor.find('.session-menu-compact').exists()).toBe(false);
    expect(cursor.find('.session-menu-compact-note').text()).toBe(
      'Cursor сжимает разговор сам — «Сжать» и «Сжатие…» у него нет',
    );
    const claude = mount(SessionMenuItems, { props: { win: null, session: session({ engine: 'claude' }) } });
    expect(claude.find('.session-menu-compact').exists()).toBe(true);
    expect(claude.find('.session-menu-compact-note').exists()).toBe(false);
  });

  it('подсказка очереди у идущего хода Cursor — правда: только на вызове инструмента', () => {
    const says = [{ status: 'queued' as const }];
    expect(sayQueueReason(session({ state: 'running' }), [], says)).toBe(SAY_TOOL_ONLY_REASON);
    // Профиль из /status главнее запасного.
    const profiles: SessionEngineProfile[] = DEFAULT_SESSION_ENGINES.map((p) =>
      p.name === 'cursor' ? { ...p, mid_turn_tool_only: false } : { ...p },
    );
    store.state.status = { accounts: [], session_engines: profiles } as unknown as StatusResponse;
    expect(sayQueueReason(session({ state: 'running' }), [], says)).toBe('сессия возьмёт после текущего шага');
  });

  it('запасной профиль Cursor: мёртвые выключатели — как в ядре', () => {
    expect(switchDeadOn('cursor', 'plan_mode')).toBe(true);
    expect(switchDeadOn('cursor', 'cross_session')).toBe(true);
    expect(switchDeadOn('cursor', 'mcp_write')).toBe(false);
    expect(switchDeadOn('claude', 'plan_mode')).toBe(false);
  });
});
