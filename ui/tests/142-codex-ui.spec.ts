// Ф74-6 (docs/architecture.md): Codex в интерфейсе — модель и усилие по `models` профиля, команда
// терминала, мёртвые выключатели «Автопилота» (`dead_switches`).
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AutopilotState, HubSession, SessionEngineProfile, StatusResponse } from '../src/api/types';
import AutopilotPanel from '../src/components/AutopilotPanel.vue';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import SessionHandoffForm from '../src/components/SessionHandoffForm.vue';
import SessionModelForm from '../src/components/SessionModelForm.vue';
import { store } from '../src/store/instance';
import { autopilotUi } from '../src/store/autopilot';
import { projectSwitchView, switchRowHidden } from '../src/util/autopilot';
import { DEFAULT_SESSION_ENGINES } from '../src/util/engineProfiles';
import {
  effortChoices,
  effortForEngine,
  modelChipText,
  modelDefaultEffort,
  sessionModelHints,
  sessionTerminalCommand,
} from '../src/util/sessions';

vi.mock('../src/api/client', async (orig) => ({
  ...(await orig<typeof import('../src/api/client')>()),
  getAutopilot: async () => autopilotUi.data,
}));

const NOW = new Date(2026, 9, 21, 12, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 7, project_id: 3, role: 'программист', role_no: 1, engine: 'codex', model: null, effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

const CODEX: SessionEngineProfile = {
  name: 'codex', label: 'Codex', short: 'Codex', efforts: ['low', 'medium', 'high', 'xhigh', 'max'],
  model_hints: ['gpt-6.1-sol'], compacts_self: false, context_known: true, mid_turn: true, available: true,
  dead_switches: ['cross_session', 'plan_mode'],
  models: [
    { id: 'gpt-6.1-sol', efforts: ['low', 'medium', 'high'], default_effort: 'medium' },
    { id: 'gpt-6.1-mini', efforts: ['low'], default_effort: null },
  ],
};

function setStatus(engines: SessionEngineProfile[] | undefined): void {
  store.state.status = { accounts: [], session_engines: engines } as unknown as StatusResponse;
}

afterEach(() => {
  store.state.status = null;
  store.state.sessions.items = {};
  autopilotUi.data = null;
  document.body.innerHTML = '';
});

describe('Ф74-6: модель и усилие Codex', () => {
  it('усилие — по выбранной модели, умолчание — default_effort', () => {
    setStatus([...DEFAULT_SESSION_ENGINES, CODEX]);
    expect(sessionModelHints('codex')).toEqual(['gpt-6.1-sol', 'gpt-6.1-mini']);
    expect(effortChoices('codex', '', 'gpt-6.1-sol')).toEqual(['low', 'medium', 'high']);
    expect(effortChoices('codex', '', 'gpt-6.1-mini')).toEqual(['low']);
    expect(effortChoices('codex', '', 'своя-модель')).toEqual(CODEX.efforts); // не из списка — уровни профиля
    expect(effortForEngine('codex', 'max', 'gpt-6.1-sol')).toBe(''); // не из списка модели выбрать нельзя
    expect(modelDefaultEffort('codex', 'gpt-6.1-sol')).toBe('medium');
    expect(modelDefaultEffort('codex', 'gpt-6.1-mini')).toBeNull();
  });

  it('нет `models` — model_hints и efforts профиля', () => {
    setStatus([...DEFAULT_SESSION_ENGINES, { ...CODEX, models: undefined }]);
    expect(sessionModelHints('codex')).toEqual(['gpt-6.1-sol']);
    expect(effortChoices('codex', '', 'gpt-6.1-sol')).toEqual(CODEX.efforts);
    setStatus([...DEFAULT_SESSION_ENGINES, { ...CODEX, models: [] }]);
    expect(sessionModelHints('codex')).toEqual(['gpt-6.1-sol']);
  });

  it('форма «Новая сессия»: Codex в списке, смена модели меняет уровни и сбрасывает лишнее', async () => {
    setStatus([...DEFAULT_SESSION_ENGINES, CODEX]);
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    const engineSelect = w.findAll('select').find((s) => s.findAll('option').some((o) => o.attributes('value') === 'codex'));
    expect(engineSelect).toBeDefined();
    await engineSelect!.setValue('codex');
    const modelInput = w.get('input[list^="session-models"]');
    await modelInput.setValue('gpt-6.1-sol');
    const effortSelect = w.findAll('select').find((s) => s.findAll('option').some((o) => o.attributes('value') === 'high'))!;
    expect(effortSelect.findAll('option').map((o) => o.attributes('value'))).toEqual(['', 'low', 'medium', 'high']);
    expect(effortSelect.findAll('option')[0].text()).toContain('(medium)');
    await effortSelect.setValue('high');
    await modelInput.setValue('gpt-6.1-mini');
    expect((effortSelect.element as HTMLSelectElement).value).toBe('');
    expect(effortSelect.findAll('option').map((o) => o.attributes('value'))).toEqual(['', 'low']);
  });

  it('передача дел и смена модели у Codex-сессии: те же уровни', async () => {
    setStatus([...DEFAULT_SESSION_ENGINES, CODEX]);
    const h = mount(SessionHandoffForm, { props: { session: session({ model: 'gpt-6.1-mini', effort: 'low' }) } });
    expect(h.findAll('.session-handoff-engine option').map((o) => o.attributes('value'))).toContain('codex');
    const m = mount(SessionModelForm, { props: { session: session({ model: 'gpt-6.1-sol', effort: 'medium' }) } });
    expect(m.findAll('.session-effort-input option').map((o) => o.attributes('value'))).toEqual(['', 'low', 'medium', 'high']);
    await m.get('.session-model-input').setValue('gpt-6.1-mini');
    expect(m.findAll('.session-effort-input option').map((o) => o.attributes('value'))).toEqual(['', 'low']);
  });

  it('чип «модель · усилие» как у Claude Code', () => {
    expect(modelChipText({ model: 'gpt-6.1-sol', effort: 'high' })).toBe('gpt-6.1-sol · high');
  });
});

describe('Ф74-6: терминал Codex', () => {
  it('codex resume <id> в папке сессии, с подпапкой', () => {
    const asleep = session({ state: 'asleep', engine_session_id: 'th-1' });
    expect(sessionTerminalCommand(asleep)).toBe('cd /p && codex resume th-1');
    expect(sessionTerminalCommand({ ...asleep, subdir: 'api' } as HubSession)).toBe('cd /p/api && codex resume th-1');
  });
});

describe('Ф74-6: мёртвые выключатели', () => {
  it('ячейка проекта: только Codex — скрыта с пометкой; смесь — видна с пометкой; без Codex — без пометки', () => {
    setStatus([...DEFAULT_SESSION_ENGINES, CODEX]);
    const s = [
      { project_id: 1, engine: 'codex', closed_ts: null },
      { project_id: 2, engine: 'codex', closed_ts: null },
      { project_id: 2, engine: 'claude', closed_ts: null },
      { project_id: 3, engine: 'claude', closed_ts: null },
      { project_id: 4, engine: 'codex', closed_ts: 5 },
    ] as const;
    expect(projectSwitchView(s, 1, 'plan_mode')).toEqual({ hidden: true, note: 'не действует на Codex' });
    expect(projectSwitchView(s, 2, 'plan_mode')).toEqual({ hidden: false, note: 'не действует на Codex' });
    expect(projectSwitchView(s, 3, 'plan_mode')).toEqual({ hidden: false, note: null });
    expect(projectSwitchView(s, 4, 'plan_mode')).toEqual({ hidden: false, note: null });
    expect(projectSwitchView(s, 1, 'push')).toEqual({ hidden: false, note: null });
    expect(switchRowHidden('plan_mode')).toBe(false); // на Claude Code действует
  });

  it('панель: у проекта только с Codex галочки plan_mode нет, у смешанного — есть и пометка', async () => {
    setStatus([...DEFAULT_SESSION_ENGINES, CODEX]);
    store.state.projects = [
      { id: 1, name: 'Один', workdir: '/a' },
      { id: 2, name: 'Два', workdir: '/b' },
    ] as unknown as typeof store.state.projects;
    store.state.sessions.items = {
      1: session({ id: 1, project_id: 1 }),
      2: session({ id: 2, project_id: 2 }),
      3: session({ id: 3, project_id: 2, engine: 'claude' }),
    };
    autopilotUi.data = {
      switches: [
        { key: 'plan_mode', group: 'g', title: 'План', manual: 'ask' },
        { key: 'push', group: 'g', title: 'Push', manual: 'ask' },
      ],
      global: {},
      projects: {},
      local: true,
    } as AutopilotState;
    autopilotUi.loaded = true;
    const w = mount(AutopilotPanel, { attachTo: document.body });
    await flushPromises();
    const plan = w.get('[data-switch="plan_mode"]');
    expect(plan.find('select[data-project-id="1"]').exists()).toBe(false);
    expect(plan.find('select[data-project-id="2"]').exists()).toBe(true);
    expect(plan.text()).toContain('не действует на Codex');
    expect(w.get('[data-switch="push"]').findAll('select')).toHaveLength(2);
    expect(w.get('[data-switch="push"]').text()).not.toContain('не действует');
  });
});
