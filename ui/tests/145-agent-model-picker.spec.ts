// Ф75-1 (docs/architecture.md.3): модель агента владельца в «Написать…» → «Мой агент» —
// строка «Модель: … ▾», выбор из списка OpenCode службы, уровень по модели, свободный ввод, «по умолчанию».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../src/api/client';
import type { OwnerAgentSettings } from '../src/api/types';
import AgentModelPicker from '../src/components/AgentModelPicker.vue';
import {
  agentModelInputError,
  agentModelLabel,
  agentSettingsError,
  agentVariantChoices,
  agentVariantFor,
} from '../src/util/agentModel';

const MODELS = [
  { id: 'deepseek/deepseek-flash', name: 'DeepSeek Flash', images: true, variants: ['low', 'high', 'max'] },
  { id: 'alibaba/qwen-max', name: 'Qwen Max', images: false, variants: [] },
];

function settings(over: Partial<OwnerAgentSettings> = {}): OwnerAgentSettings {
  return {
    model: null,
    variant: null,
    set_ts: null,
    default: { model: 'deepseek/deepseek-flash', variant: 'max' },
    models: MODELS,
    models_ts: 1,
    ...over,
  };
}

const api = vi.hoisted(() => ({
  current: null as OwnerAgentSettings | null,
  puts: [] as Array<{ model: string | null; variant: string | null }>,
  fail: null as Error | null,
}));

vi.mock('../src/api/client', async (orig) => ({
  ...(await orig<typeof import('../src/api/client')>()),
  getOwnerAgentSettings: async () => api.current,
  putOwnerAgentSettings: async (body: { model: string | null; variant: string | null }) => {
    api.puts.push(body);
    if (api.fail !== null) throw api.fail;
    const base = api.current as OwnerAgentSettings;
    api.current = { ...base, model: body.model, variant: body.model === null ? null : body.variant, set_ts: 5 };
    return api.current;
  },
}));

afterEach(() => {
  api.current = null;
  api.puts = [];
  api.fail = null;
  document.body.innerHTML = '';
});

describe('подписи и уровни', () => {
  it('строка: выбор владельца или умолчание с пометкой', () => {
    expect(agentModelLabel(settings())).toBe('deepseek/deepseek-flash · max (по умолчанию)');
    expect(agentModelLabel(settings({ model: 'alibaba/qwen-max', variant: null }))).toBe('alibaba/qwen-max');
    expect(agentModelLabel(settings({ model: 'deepseek/deepseek-flash', variant: 'low' }))).toBe('deepseek/deepseek-flash · low');
  });

  it('уровни — из списка модели; модели нет в списке — свободный ввод', () => {
    expect(agentVariantChoices(MODELS, 'deepseek/deepseek-flash')).toEqual(['low', 'high', 'max']);
    expect(agentVariantChoices(MODELS, 'alibaba/qwen-max')).toEqual([]);
    expect(agentVariantChoices(MODELS, 'openrouter/x')).toBeNull();
    expect(agentVariantFor(MODELS, 'alibaba/qwen-max', 'max')).toBe('');
    expect(agentVariantFor(MODELS, 'deepseek/deepseek-flash', 'high')).toBe('high');
    expect(agentVariantFor(MODELS, 'openrouter/x', 'turbo')).toBe('turbo');
  });

  it('проверка ввода и тексты ошибок', () => {
    expect(agentModelInputError('')).toBeNull();
    expect(agentModelInputError('openrouter/some:model')).toBeNull();
    expect(agentModelInputError('без слеша')).not.toBeNull();
    expect(agentSettingsError(new ApiError(403, 'forbidden', null))).toContain('только из интерфейса');
    expect(agentSettingsError(new ApiError(422, 'x', null))).toContain('ядро не приняло: x');
  });
});

describe('AgentModelPicker', () => {
  it('выбор из списка с уровнем — PUT и новая строка сразу', async () => {
    api.current = settings();
    const w = mount(AgentModelPicker, { attachTo: document.body });
    await flushPromises();
    const line = w.get('[data-test="agent-model-line"]');
    expect(line.text()).toBe('Модель: deepseek/deepseek-flash · max (по умолчанию) ▾');
    await line.trigger('click');
    const options = w.findAll('#agent-model-list option').map((o) => o.attributes('value'));
    expect(options).toEqual(['deepseek/deepseek-flash', 'alibaba/qwen-max']);
    expect(w.findAll('[data-test="agent-variant-select"] option').map((o) => o.text())).toEqual([
      'по умолчанию', 'low', 'high', 'max',
    ]);
    await w.get('[data-test="agent-variant-select"]').setValue('low');
    await w.get('[data-test="agent-model-form"]').trigger('submit');
    await flushPromises();
    expect(api.puts).toEqual([{ model: 'deepseek/deepseek-flash', variant: 'low' }]);
    expect(w.get('[data-test="agent-model-line"]').text()).toBe('Модель: deepseek/deepseek-flash · low ▾');
    expect(w.get('[data-test="agent-model-saved"]').text()).toContain('со следующего хода');
    expect(w.find('[data-test="agent-model-form"]').exists()).toBe(false);
  });

  it('модель без уровней сбрасывает уровень; свободный ввод — поле уровня текстом', async () => {
    api.current = settings();
    const w = mount(AgentModelPicker, { attachTo: document.body });
    await flushPromises();
    await w.get('[data-test="agent-model-line"]').trigger('click');
    await w.get('[data-test="agent-model-input"]').setValue('alibaba/qwen-max');
    await flushPromises();
    expect(w.findAll('[data-test="agent-variant-select"] option').map((o) => o.text())).toEqual(['по умолчанию']);
    await w.get('[data-test="agent-model-input"]').setValue('openrouter/some:model');
    await flushPromises();
    expect(w.find('[data-test="agent-variant-select"]').exists()).toBe(false);
    await w.get('[data-test="agent-variant-input"]').setValue('turbo');
    expect(w.text()).toContain('нет в списке OpenCode');
    await w.get('[data-test="agent-model-form"]').trigger('submit');
    await flushPromises();
    expect(api.puts).toEqual([{ model: 'openrouter/some:model', variant: 'turbo' }]);
  });

  it('негодное имя не уходит; «по умолчанию» снимает выбор; отказ ядра виден', async () => {
    api.current = settings({ model: 'alibaba/qwen-max', variant: null, set_ts: 3 });
    const w = mount(AgentModelPicker, { attachTo: document.body });
    await flushPromises();
    await w.get('[data-test="agent-model-line"]').trigger('click');
    await w.get('[data-test="agent-model-input"]').setValue('без слеша');
    expect(w.get('[data-test="agent-model-save"]').attributes('disabled')).toBeDefined();
    await w.get('[data-test="agent-model-form"]').trigger('submit');
    await flushPromises();
    expect(api.puts).toEqual([]);

    await w.get('[data-test="agent-model-reset"]').trigger('click');
    await flushPromises();
    expect(api.puts).toEqual([{ model: null, variant: null }]);
    expect(w.get('[data-test="agent-model-line"]').text()).toContain('(по умолчанию)');

    api.fail = new ApiError(403, 'forbidden', null);
    await w.get('[data-test="agent-model-line"]').trigger('click');
    await w.get('[data-test="agent-model-form"]').trigger('submit');
    await flushPromises();
    expect(w.get('[data-test="agent-model-error"]').text()).toContain('только из интерфейса');
  });

  it('пустой список моделей — подсказка о свободном вводе', async () => {
    api.current = settings({ models: [], models_ts: null });
    const w = mount(AgentModelPicker, { attachTo: document.body });
    await flushPromises();
    await w.get('[data-test="agent-model-line"]').trigger('click');
    expect(w.text()).toContain('ещё не прислала');
    expect(w.find('[data-test="agent-variant-input"]').exists()).toBe(true);
  });
});

describe('AgentModelPicker: движок агента не OpenCode (Ф78-2c)', () => {
  it('Claude Code — модели движка, имя без слеша уходит, в строке движок', async () => {
    api.current = settings({
      engine: 'claude',
      model: null,
      default: { model: 'sonnet', variant: null },
      models: [
        { id: 'sonnet', name: 'Sonnet', images: true, variants: [] },
        { id: 'opus', name: 'Opus', images: true, variants: ['high'] },
      ],
    });
    const w = mount(AgentModelPicker, { attachTo: document.body });
    await flushPromises();
    const line = w.get('[data-test="agent-model-line"]');
    expect(line.text()).toBe('Модель (Claude Code): sonnet (по умолчанию) ▾');
    await line.trigger('click');
    expect(w.findAll('#agent-model-list option').map((o) => o.attributes('value'))).toEqual(['sonnet', 'opus']);
    await w.get('[data-test="agent-model-input"]').setValue('opus');
    await flushPromises();
    expect(w.findAll('[data-test="agent-variant-select"] option').map((o) => o.text())).toEqual(['по умолчанию', 'high']);
    expect(w.get('[data-test="agent-model-save"]').attributes('disabled')).toBeUndefined();
    await w.get('[data-test="agent-variant-select"]').setValue('high');
    await w.get('[data-test="agent-model-form"]').trigger('submit');
    await flushPromises();
    expect(api.puts).toEqual([{ model: 'opus', variant: 'high' }]);
  });

  it('модель с пробелом не уходит и у не-OpenCode', async () => {
    api.current = settings({ engine: 'codex', default: { model: 'gpt-6-luna', variant: 'low' } });
    const w = mount(AgentModelPicker, { attachTo: document.body });
    await flushPromises();
    await w.get('[data-test="agent-model-line"]').trigger('click');
    await w.get('[data-test="agent-model-input"]').setValue('gpt 6');
    expect(w.get('[data-test="agent-model-save"]').attributes('disabled')).toBeDefined();
    await w.get('[data-test="agent-model-input"]').setValue('gpt-6-luna');
    expect(w.get('[data-test="agent-model-save"]').attributes('disabled')).toBeUndefined();
  });
});
