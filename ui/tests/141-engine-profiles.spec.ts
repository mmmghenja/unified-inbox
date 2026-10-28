// Ф74-1 (docs/architecture.md): движки сессий хаба — из профилей `/status` (`session_engines`);
// старое ядро поля не отдаёт — запасные значения, равные прежним. Заготовка (`available: false`) не предлагается.
import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it } from 'vitest';

import type { HubSession, SessionEngineProfile, StatusResponse } from '../src/api/types';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import SessionHandoffForm from '../src/components/SessionHandoffForm.vue';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import { store } from '../src/store/instance';
import { engineLabel } from '../src/store/peerMsgs';
import {
  DEFAULT_SESSION_ENGINES,
  engineProfile,
  sessionEngineChoices,
  sessionEngineLabel,
  sessionEngineProfiles,
} from '../src/util/engineProfiles';
import { sayQueueReason } from '../src/util/sayQueue';
import {
  SESSION_EFFORTS_BY_ENGINE,
  SESSION_MODEL_HINTS,
  effortChoices,
  effortForEngine,
  sessionModelHints,
} from '../src/util/sessions';

const NOW = new Date(2026, 9, 21, 12, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 7, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: null, effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

function serverProfiles(): SessionEngineProfile[] {
  return [
    ...DEFAULT_SESSION_ENGINES.map((p) => ({ ...p })),
    {
      name: 'codex', label: 'Codex', short: 'Codex', efforts: [], model_hints: [], compacts_self: false,
      context_known: true, mid_turn: true, available: false,
    },
  ];
}

function setStatus(engines?: SessionEngineProfile[]): void {
  store.state.status = { accounts: [], session_engines: engines } as unknown as StatusResponse;
}

afterEach(() => {
  store.state.status = null;
  document.body.innerHTML = '';
});

describe('Ф74-1: профили движков сессий хаба', () => {
  it('запасные значения равны прежним константам', () => {
    store.state.status = null;
    expect(sessionEngineProfiles().map((p) => p.name)).toEqual(['claude', 'opencode', 'cursor']);
    for (const p of DEFAULT_SESSION_ENGINES) {
      const name = p.name as keyof typeof SESSION_MODEL_HINTS;
      expect(p.model_hints).toEqual(SESSION_MODEL_HINTS[name]);
      expect(p.efforts).toEqual([...SESSION_EFFORTS_BY_ENGINE[name]]);
    }
    expect(sessionEngineLabel('claude')).toBe('Claude Code');
    expect(sessionEngineLabel('opencode')).toBe('OpenCode');
    expect(sessionEngineLabel('cursor')).toBe('Cursor');
    expect(engineLabel(undefined)).toBe('Claude Code');
    expect(engineLabel('nope')).toBe('Claude Code');
  });

  it('старое ядро (нет поля или пустой список) — запасные', () => {
    setStatus(undefined);
    expect(sessionEngineChoices().map((p) => p.name)).toEqual(['claude', 'opencode', 'cursor']);
    setStatus([]);
    expect(sessionEngineChoices().map((p) => p.name)).toEqual(['claude', 'opencode', 'cursor']);
    expect(sessionEngineProfiles([{ bad: 1 }]).map((p) => p.name)).toEqual(['claude', 'opencode', 'cursor']);
  });

  it('из /status: заготовка не предлагается, подпись и подсказки — из профиля', () => {
    const profiles = serverProfiles();
    profiles[0] = { ...profiles[0], label: 'Claude Code*', model_hints: ['opus'], efforts: ['high'] };
    setStatus(profiles);
    expect(sessionEngineChoices().map((p) => p.name)).toEqual(['claude', 'opencode', 'cursor']);
    expect(sessionEngineLabel('claude')).toBe('Claude Code*');
    expect(sessionEngineLabel('codex')).toBe('Codex');
    expect(sessionModelHints('claude')).toEqual(['opus']);
    expect(effortChoices('claude', 'max')).toEqual(['high', 'max']);
    expect(effortForEngine('claude', 'low')).toBe('');
    expect(engineProfile('codex')?.available).toBe(false);
  });

  it('формы предлагают движки из профиля, без заготовки', () => {
    const profiles = serverProfiles();
    profiles[2] = { ...profiles[2], label: 'Cursor CLI' };
    setStatus(profiles);
    const handoff = mount(SessionHandoffForm, { props: { session: session() } });
    const options = handoff.findAll('.session-handoff-engine option');
    expect(options.map((o) => o.attributes('value'))).toEqual(['claude', 'opencode', 'cursor']);
    expect(options[2].text()).toBe('Cursor CLI');
    const created = mount(NewSessionForm, { props: { projectId: 3 } });
    const values = created.findAll('.session-new-engine option').map((o) => o.attributes('value'));
    expect(values).toEqual(['claude', 'opencode', 'cursor']);
    expect((created.find('.session-new-engine').element as HTMLSelectElement).value).toBe('claude');
  });

  it('«Сжать» прячется по compacts_self профиля', () => {
    const profiles = serverProfiles();
    profiles[1] = { ...profiles[1], compacts_self: true };
    setStatus(profiles);
    const oc = mount(SessionMenuItems, { props: { win: null, session: session({ engine: 'opencode' }) } });
    expect(oc.find('.session-menu-compact').exists()).toBe(false);
    const claude = mount(SessionMenuItems, { props: { win: null, session: session() } });
    expect(claude.find('.session-menu-compact').exists()).toBe(true);
  });

  it('подсказка очереди во время хода — по mid_turn: Cursor и Claude берут, OpenCode нет', () => {
    const says = [{ status: 'queued' as const }];
    const running = (engine: string) => session({ state: 'running', engine: engine as HubSession['engine'] });
    // Ф74-8: Cursor берёт только на вызове инструмента (`mid_turn_tool_only`).
    expect(sayQueueReason(running('cursor'), [], says)).toBe('возьмёт при следующем вызове инструмента, иначе — следующим ходом');
    expect(sayQueueReason(running('claude'), [], says)).toBe('сессия возьмёт после текущего шага');
    expect(sayQueueReason(running('opencode'), [], says)).toBe('уйдёт, когда закончится ход');
  });
});
