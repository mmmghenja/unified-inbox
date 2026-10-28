// Ф76-4б: раздел «Мой агент» в «Автопилоте» — правила, образцы, журнал решений агента, модель проекта;
// строка основания на закрытой карточке.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../src/api/client';
import type { AgentDecision, PatternsState, OwnerAgentSettings } from '../src/api/types';
import AgentPatternsSection from '../src/components/AgentPatternsSection.vue';
import OwnerResults from '../src/components/OwnerResults.vue';
import { store } from '../src/store/instance';
import { basisLabel, toggleKey } from '../src/util/agentPatterns';

const GENERAL: OwnerAgentSettings = {
  model: null,
  variant: null,
  set_ts: null,
  default: { model: 'deepseek/deepseek-flash', variant: 'max' },
  models: [{ id: 'deepseek/deepseek-flash', name: 'DeepSeek Flash', images: true, variants: ['low', 'max'] }],
  models_ts: 1,
};

function pstate(over: Partial<PatternsState> = {}): PatternsState {
  return {
    project_id: 0,
    rules: 'пуш только по слову',
    keep: ['k0'],
    drop: [],
    always_me: [],
    min_count: 3,
    window_days: 30,
    patterns: [
      { key: 'k1', line: 'git/push → да, 5 раз', count: 5, last_ts: 1760000000000, status: 'active', examples: ['пуш? → да'] },
      { key: 'k0', line: 'turn/deploy → нет, 3 раза', count: 3, last_ts: 1760000000000, status: 'young' },
    ],
    ...over,
  };
}

const api = vi.hoisted(() => ({
  state: null as unknown,
  decisions: null as unknown,
  puts: [] as Array<{ id: number; body: unknown }>,
  wrongs: [] as number[],
  projectPuts: [] as Array<{ id: number; body: unknown }>,
  wrongRes: { id: 1, wrong_ts: 9, dropped: false } as { id: number; wrong_ts: number | null; dropped: boolean },
  closed: [] as unknown[],
}));

vi.mock('../src/api/client', async (orig) => ({
  ...(await orig<typeof import('../src/api/client')>()),
  getOwnerAgentSettings: async () => GENERAL,
  getOwnerProjectModel: async () => ({ model: null, variant: null }),
  putOwnerProjectModel: async (id: number, body: unknown) => {
    api.projectPuts.push({ id, body });
    return body;
  },
  getPatterns: async () => api.state,
  putPatterns: async (id: number, body: unknown) => {
    api.puts.push({ id, body });
    return {};
  },
  getAgentDecisions: async () => {
    if (api.decisions === null) throw new (await import('../src/api/client')).ApiError(404, 'нет', null);
    return { items: api.decisions };
  },
  markDecisionWrong: async (id: number) => {
    api.wrongs.push(id);
    return api.wrongRes;
  },
  getOwnerClosed: async () => ({ items: api.closed }),
}));

afterEach(() => {
  api.state = null;
  api.decisions = null;
  api.puts = [];
  api.wrongs = [];
  api.projectPuts = [];
  api.closed = [];
  api.wrongRes = { id: 1, wrong_ts: 9, dropped: false };
  document.body.innerHTML = '';
});

function decision(over: Partial<AgentDecision> = {}): AgentDecision {
  return {
    id: 1,
    ts: 1760000000000,
    ref: 'r1',
    project_id: 1,
    kind: 'permission',
    role: 'программист',
    action: 'да',
    verdict: 'yes',
    text: 'git push',
    basis: 'pattern:git/push',
    pattern_key: 'git/push',
    wrong_ts: null,
    ...over,
  };
}

async function mountSection() {
  const w = mount(AgentPatternsSection, { attachTo: document.body });
  await flushPromises();
  return w;
}

describe('перевод основания', () => {
  it('rule / pattern / similar / card / none', () => {
    expect(basisLabel('rule:не пушить')).toBe('по правилу не пушить');
    expect(basisLabel('rule:')).toBe('по правилу');
    expect(basisLabel('pattern:git/push')).toBe('по образцу git/push');
    expect(basisLabel('similar:18.10')).toBe('по похожему решению 18.10');
    expect(basisLabel('card')).toBe('из карточки');
    expect(basisLabel('none')).toBe('без основания');
    expect(basisLabel(null)).toBe('без основания');
  });

  it('toggleKey даёт полный новый список', () => {
    expect(toggleKey(['a'], 'b')).toEqual(['a', 'b']);
    expect(toggleKey(['a', 'b'], 'a')).toEqual(['b']);
  });
});

describe('раздел «Мой агент»', () => {
  it('загружает правила, образцы и порог', async () => {
    api.state = pstate();
    const w = await mountSection();
    expect((w.find('[data-test="ap-rules"]').element as HTMLTextAreaElement).value).toBe('пуш только по слову');
    expect(w.findAll('[data-test="ap-pattern"]')).toHaveLength(2);
    expect(w.find('[data-test="ap-threshold"]').text()).toBe('образец — от 3 одинаковых решений за 30 дней');
    expect(w.text()).toContain('git/push → да, 5 раз');
  });

  it('«Сохранить» шлёт правила выбранному проекту', async () => {
    store.state.projects = [{ id: 7, name: 'Альфа' } as never];
    api.state = pstate();
    const w = await mountSection();
    await w.find('[data-test="ap-rules"]').setValue('новое правило');
    await w.find('[data-test="ap-rules-save"]').trigger('click');
    await flushPromises();
    expect(api.puts).toEqual([{ id: 0, body: { rules: 'новое правило' } }]);
    await w.find('[data-test="ap-scope"]').setValue('7');
    await flushPromises();
    await w.find('[data-test="ap-rules"]').setValue('  ');
    await w.find('[data-test="ap-rules-save"]').trigger('click');
    await flushPromises();
    expect(api.puts[1]).toEqual({ id: 7, body: { rules: null } });
  });

  it('кнопки образца шлют полный новый список', async () => {
    api.state = pstate();
    const w = await mountSection();
    const first = w.findAll('[data-test="ap-pattern"]')[0];
    await first.find('[data-test="ap-keep"]').trigger('click');
    await flushPromises();
    await first.find('[data-test="ap-drop"]').trigger('click');
    await flushPromises();
    await first.find('[data-test="ap-always"]').trigger('click');
    await flushPromises();
    expect(api.puts.map((p) => p.body)).toEqual([{ keep: ['k0', 'k1'] }, { drop: ['k1'] }, { always_me: ['k1'] }]);
  });

  it('повторное «оставить» снимает ключ', async () => {
    api.state = pstate();
    const w = await mountSection();
    await w.findAll('[data-test="ap-pattern"]')[1].find('[data-test="ap-keep"]').trigger('click');
    await flushPromises();
    expect(api.puts[0].body).toEqual({ keep: [] });
  });

  it('примеры раскрываются по клику', async () => {
    api.state = pstate();
    const w = await mountSection();
    expect(w.find('[data-test="ap-example"]').exists()).toBe(false);
    await w.find('[data-test="ap-examples-toggle"]').trigger('click');
    expect(w.find('[data-test="ap-example"]').text()).toBe('пуш? → да');
  });

  it('журнал: основание по-русски, «неверно», помеченная серым', async () => {
    api.state = pstate();
    api.decisions = [decision(), decision({ id: 2, basis: 'rule:x', wrong_ts: 5 })];
    api.wrongRes = { id: 1, wrong_ts: 9, dropped: true };
    const w = await mountSection();
    const rows = w.findAll('[data-test="ap-decision"]');
    expect(rows[0].find('[data-test="ap-basis"]').text()).toBe('по образцу git/push');
    expect(rows[1].find('[data-test="ap-wrong-mark"]').text()).toBe('помечено неверным');
    expect(rows[1].find('[data-test="ap-wrong"]').exists()).toBe(false);
    await rows[0].find('[data-test="ap-wrong"]').trigger('click');
    await flushPromises();
    expect(api.wrongs).toEqual([1]);
    expect(w.findAll('[data-test="ap-decision"]')[0].find('[data-test="ap-wrong-mark"]').text()).toBe(
      'помечено неверным · образец убран',
    );
  });

  it('журнала ещё нет (404) — раздел пуст без ошибки', async () => {
    api.state = pstate();
    api.decisions = null;
    const w = await mountSection();
    expect(w.find('[data-test="ap-decisions-empty"]').exists()).toBe(true);
    expect(w.find('[data-test="ap-error"]').exists()).toBe(false);
    expect(ApiError).toBeDefined();
  });

  it('модель проекта: сохранение уходит на ручку проекта', async () => {
    store.state.projects = [{ id: 7, name: 'Альфа' } as never];
    api.state = pstate();
    const w = await mountSection();
    await w.find('[data-test="ap-scope"]').setValue('7');
    await flushPromises();
    expect(w.find('[data-test="agent-model-line"]').text()).toContain('как общая');
    await w.find('[data-test="agent-model-line"]').trigger('click');
    await w.find('[data-test="agent-model-input"]').setValue('deepseek/deepseek-flash');
    await w.find('[data-test="agent-model-form"]').trigger('submit');
    await flushPromises();
    expect(api.projectPuts).toEqual([{ id: 7, body: { model: 'deepseek/deepseek-flash', variant: 'max' } }]);
  });
});

describe('карточка, решённая агентом', () => {
  it('в итогах строка основания', async () => {
    api.closed = [
      { id: 1, ref: 'r', kind: 'permission', project_id: 1, closed_ts: 1760000000000, closed_by: 'agent', errand_result: null, basis: 'pattern:git/push' },
    ];
    const w = mount(OwnerResults, { attachTo: document.body });
    const details = w.find('details').element as HTMLDetailsElement;
    details.open = true;
    await w.find('details').trigger('toggle');
    await flushPromises();
    expect(w.find('[data-test="owner-result-basis"]').text()).toBe('по образцу git/push');
  });
});
