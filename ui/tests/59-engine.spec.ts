// Ф16-1 B → Ф78-2c: выбор «движок · модель · усилие» в UI — чип в меню «⋯» («<движок> · <модель>»), панель «Движок»
// с двумя выборами («Мой агент» — `PUT /engine`, «Факты о людях» — `PUT /engine/facts`, «как у агента» —
// `engine: null`), подтверждение, 403/409/422 ядра, телефон без запретов. Плюс чистые функции `util/engine`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { EngineInfo, EngineName, EngineStatus, StatusResponse } from '../src/api/types';
import EnginePanel from '../src/components/EnginePanel.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';
import {
  afterModel,
  choiceLabel,
  confirmAgentText,
  confirmFactsText,
  defaultChoice,
  effortsFor,
  ENGINE_LABEL,
  factsChoice,
  optionDisabled,
  optionReason,
  PHONE_HINT,
  switchedNotice,
  switchError,
} from '../src/util/engine';

const val = (w: { element: unknown }): string => (w.element as HTMLSelectElement).value;
const chk = (w: { element: unknown }): boolean => (w.element as HTMLInputElement).checked;

const NOT_READY = 'OpenCode в хабе ещё не готов';

function info(name: EngineName, over: Partial<EngineInfo> = {}): EngineInfo {
  const base: Record<EngineName, EngineInfo> = {
    claude: {
      name: 'claude', label: 'Claude Code', available: true, reason: null,
      models: [
        { id: 'sonnet', name: 'Sonnet', images: true, efforts: ['low', 'medium', 'high'] },
        { id: 'opus', name: 'Opus', images: true, efforts: ['high', 'xhigh'] },
      ],
      efforts: ['low', 'medium', 'high'], default_model: 'sonnet', default_effort: null, model: null, effort: null,
    },
    opencode: {
      name: 'opencode', label: 'OpenCode', available: true, reason: null,
      models: [
        { id: 'deepseek/deepseek-flash', name: 'DeepSeek Flash', images: false, efforts: ['low', 'max'] },
        { id: 'alibaba/qwen-vl', name: 'Qwen VL', images: true, efforts: ['low', 'max'] },
      ],
      efforts: ['low', 'max'], default_model: 'deepseek/deepseek-flash', default_effort: 'max', model: null, effort: null,
    },
    codex: {
      name: 'codex', label: 'Codex', available: false, reason: 'codex: нет входа — войди в терминале',
      models: [{ id: 'gpt-6-luna', name: 'GPT 6 Luna', images: true, efforts: ['low', 'high'] }],
      efforts: ['low', 'high'], default_model: 'gpt-6-luna', default_effort: 'low', model: null, effort: null,
    },
    cursor: {
      name: 'cursor', label: 'Cursor', available: true, reason: null,
      models: [{ id: 'grok-4.7', name: 'Grok 4.7', images: false, efforts: ['high', 'xhigh'] }],
      efforts: ['high', 'xhigh'], default_model: 'grok-4.7', default_effort: 'high', model: null, effort: null,
    },
  };
  return { ...base[name], ...over };
}

function engineStatus(over: Partial<EngineStatus> = {}): EngineStatus {
  return {
    engine: 'opencode',
    model: 'deepseek/deepseek-flash',
    effort: 'max',
    changed_ts: null,
    engines: [info('claude'), info('opencode'), info('codex'), info('cursor')],
    facts: { engine: 'opencode', model: 'deepseek/deepseek-flash', effort: 'max', follows_agent: true },
    ...over,
  };
}

function jsonResponse(body: unknown, statusCode = 200): Response {
  return new Response(JSON.stringify(body), { status: statusCode, headers: { 'content-type': 'application/json' } });
}

function resetStore(): void {
  store.state.projects = [];
  store.state.status = null;
  store.state.homeOpen = true;
  store.state.currentProjectId = null;
  store.state.currentChannelId = null;
  store.state.openThread = null;
  store.state.remoteSession = false;
  store.state.phoneTab = 'wait';
  store.state.home = { react: [], drafts: [], runs: [], sent: [], loadedAt: null, error: null };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetStore();
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  resetStore();
});

// --- 1. чип в меню ------------------------------------------------------------------------------------------

async function openMenu(w: { get: (selector: string) => { trigger: (event: string) => Promise<void> } }): Promise<void> {
  await w.get('.top-menu-button').trigger('click');
}

describe('Ф78-2c: чип «движок · модель» в меню «⋯»', () => {
  it('движок и модель из `/status`; модели нет — только движок', async () => {
    const without = mount(TopBar, { attachTo: document.body });
    await openMenu(without);
    expect(without.get('[data-testid="engine-chip"]').text()).toBe('Движок: Claude Code');
    without.unmount();

    store.state.status = { engine: 'opencode', engine_model: 'deepseek/deepseek-flash' } as unknown as StatusResponse;
    const withModel = mount(TopBar, { attachTo: document.body });
    await openMenu(withModel);
    expect(withModel.get('[data-testid="engine-chip"]').text()).toBe('Движок: OpenCode · deepseek/deepseek-flash');
    withModel.unmount();
  });

  it('клик открывает панель движка и закрывает «Раскладку» и «Доступ»', async () => {
    vi.spyOn(api, 'getEngine').mockResolvedValue(engineStatus());
    const w = mount(TopBar, { attachTo: document.body });
    await openMenu(w);
    await w.get('.access-open').trigger('click');
    expect(w.find('.access-panel:not(.mapping-panel):not(.engine-panel)').exists()).toBe(true);
    await openMenu(w);
    await w.get('.mapping-open').trigger('click');
    expect(w.find('.mapping-panel').exists()).toBe(true);
    await openMenu(w);
    await w.get('[data-testid="engine-chip"]').trigger('click');
    await flushPromises();
    expect(w.find('.engine-panel').exists()).toBe(true);
    expect(w.find('.mapping-panel').exists()).toBe(false);
    // Ф80-1 B1: нажатие по «⋯» — «мимо» панели: она закрывается сама.
    await openMenu(w);
    expect(w.find('.engine-panel').exists()).toBe(false);
    w.unmount();
  });

  it('удалённая сессия (телефон) — пункт активен и панель открывается и держится', async () => {
    store.state.remoteSession = true;
    vi.spyOn(api, 'getEngine').mockResolvedValue(engineStatus());
    const w = mount(TopBar, { attachTo: document.body });
    await openMenu(w);
    const chip = w.get('[data-testid="engine-chip"]');
    expect(chip.attributes('disabled')).toBeUndefined();
    await chip.trigger('click');
    await flushPromises();
    expect(w.find('.engine-panel').exists()).toBe(true);
    expect(w.find('.engine-agent').exists()).toBe(true);
    w.unmount();

    const phone = mount(PhoneShell, { attachTo: document.body });
    await openMenu(phone);
    expect(phone.get('[data-testid="engine-chip"]').attributes('disabled')).toBeUndefined();
    phone.unmount();
  });
});

// --- 2. панель ----------------------------------------------------------------------------------------------

describe('Ф78-2c: панель «Движок»', () => {
  function mountPanel(status: EngineStatus = engineStatus()) {
    vi.spyOn(api, 'getEngine').mockResolvedValue(status);
    vi.spyOn(store, 'refreshStatus').mockResolvedValue(undefined);
    return mount(EnginePanel, { attachTo: document.body });
  }
  type W = ReturnType<typeof mountPanel>;

  const agentBlock = (w: W) => w.get('[data-block="agent"]');
  const factsBlock = (w: W) => w.get('[data-block="facts"]');
  const engineBtn = (block: ReturnType<typeof agentBlock>, engine: string) =>
    block.get(`.engine-option[data-engine="${engine}"] .engine-choose`);

  it('два блока: агент и факты; четыре движка, текущий отмечен', async () => {
    const w = mountPanel();
    await flushPromises();
    expect(agentBlock(w).findAll('.engine-option')).toHaveLength(4);
    expect(engineBtn(agentBlock(w), 'opencode').text()).toBe('OpenCode ✓');
    expect(val(agentBlock(w).get('.engine-model'))).toBe('deepseek/deepseek-flash');
    expect(val(agentBlock(w).get('.engine-effort'))).toBe('max');
    expect(factsBlock(w).find('.engine-option').exists()).toBe(false); // «как у агента» — выбор скрыт
    expect(chk(factsBlock(w).findAll('input[type="radio"]')[0]!)).toBe(true);
    w.unmount();
  });

  it('панель не падает, если ядро не присылает `roles` (и старых полей)', async () => {
    const w = mountPanel(engineStatus());
    await flushPromises();
    expect(w.find('.engine-error').exists()).toBe(false);
    expect(w.text()).not.toContain('по ролям');
    w.unmount();
  });

  it('недоступный движок — серый и с причиной ядра, выбрать нельзя', async () => {
    const w = mountPanel();
    await flushPromises();
    const li = agentBlock(w).get('.engine-option[data-engine="codex"]');
    expect(li.classes()).toContain('engine-option--off');
    expect(li.get('.engine-reason').text()).toBe('codex: нет входа — войди в терминале');
    expect(engineBtn(agentBlock(w), 'codex').attributes('disabled')).toBeDefined();
    w.unmount();
  });

  it('агент: движок → модель → усилие, подтверждение, PUT /engine с моделью и усилием', async () => {
    const w = mountPanel();
    await flushPromises();
    const put = vi
      .spyOn(api, 'putEngine')
      .mockResolvedValue({
        ...engineStatus({ engine: 'claude', model: 'opus', effort: 'xhigh' }),
        old_engine: 'opencode',
      });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await engineBtn(agentBlock(w), 'claude').trigger('click');
    // выбор Claude: модель по умолчанию sonnet, усилие подогнано под её список
    expect(val(agentBlock(w).get('.engine-model'))).toBe('sonnet');
    await agentBlock(w).get('.engine-model').setValue('opus');
    expect(agentBlock(w).findAll('.engine-effort option').map((o) => o.text())).toEqual(['по умолчанию', 'high', 'xhigh']);
    await agentBlock(w).get('.engine-effort').setValue('xhigh');
    await agentBlock(w).get('.engine-apply').trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenCalledTimes(1);
    const text = confirm.mock.calls[0]![0] as string;
    expect(text).toContain('Агент владельца перейдёт на Claude Code · opus · xhigh');
    expect(text).toContain('Факты о людях идут за агентом');
    expect(put).toHaveBeenCalledWith('claude', { model: 'opus', effort: 'xhigh' });
    expect(w.get('.engine-notice').text()).toBe('Агент: Claude Code · opus · xhigh.');
    w.unmount();
  });

  it('отказ в подтверждении не шлёт PUT; факты «своё» в подтверждении агента не упоминаются', async () => {
    const w = mountPanel(
      engineStatus({ facts: { engine: 'cursor', model: 'grok-4.7', effort: 'high', follows_agent: false } }),
    );
    await flushPromises();
    const put = vi.spyOn(api, 'putEngine');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await engineBtn(agentBlock(w), 'claude').trigger('click');
    await agentBlock(w).get('.engine-apply').trigger('click');
    await flushPromises();
    expect(confirm.mock.calls[0]![0]).not.toContain('Факты');
    expect(put).not.toHaveBeenCalled();
    w.unmount();
  });

  it('«Применить» неактивна, пока выбор не изменён', async () => {
    const w = mountPanel();
    await flushPromises();
    expect(agentBlock(w).get('.engine-apply').attributes('disabled')).toBeDefined();
    expect(factsBlock(w).get('.engine-apply').attributes('disabled')).toBeDefined();
    w.unmount();
  });

  it('модель агента без картинок — пометка про «Реши»; у фактов её нет', async () => {
    const w = mountPanel();
    await flushPromises();
    expect(agentBlock(w).get('.engine-no-images').text()).toContain('«Реши» с браузером на ней не сработает');
    await agentBlock(w).get('.engine-model').setValue('alibaba/qwen-vl');
    expect(agentBlock(w).find('.engine-no-images').exists()).toBe(false);
    await factsBlock(w).findAll('input[type="radio"]')[1]!.setValue(true);
    expect(factsBlock(w).find('.engine-no-images').exists()).toBe(false);
    w.unmount();
  });

  it('факты: «своё» → PUT /engine/facts с движком, моделью и усилием', async () => {
    const w = mountPanel();
    await flushPromises();
    const put = vi.spyOn(api, 'putEngineFacts').mockResolvedValue(
      engineStatus({ facts: { engine: 'cursor', model: 'grok-4.7', effort: 'xhigh', follows_agent: false } }),
    );
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await factsBlock(w).findAll('input[type="radio"]')[1]!.setValue(true);
    expect(factsBlock(w).findAll('.engine-option')).toHaveLength(4);
    await engineBtn(factsBlock(w), 'cursor').trigger('click');
    await factsBlock(w).get('.engine-effort').setValue('xhigh');
    await factsBlock(w).get('.engine-apply').trigger('click');
    await flushPromises();
    expect(confirm.mock.calls[0]![0]).toBe('Факты о людях перейдут на Cursor · grok-4.7 · xhigh.');
    expect(put).toHaveBeenCalledWith('cursor', { model: 'grok-4.7', effort: 'xhigh' });
    expect(w.get('.engine-notice').text()).toBe('Факты: Cursor · grok-4.7 · xhigh.');
    // ядро записало своё — «своё» отмечено, выбор показан
    expect(chk(factsBlock(w).findAll('input[type="radio"]')[1]!)).toBe(true);
    w.unmount();
  });

  it('факты берут модели и умолчание из `facts_*` (у Claude есть haiku, у агента — нет)', async () => {
    const claude = info('claude', {
      facts_models: [
        { id: 'haiku', name: 'Haiku', images: true, efforts: ['low', 'medium', 'high'] },
        { id: 'sonnet', name: 'Sonnet', images: true, efforts: ['low', 'medium', 'high'] },
      ],
      facts_default_model: 'haiku',
      facts_default_effort: null,
    });
    const w = mountPanel(
      engineStatus({ engines: [claude, info('opencode'), info('codex'), info('cursor')] }),
    );
    await flushPromises();
    await factsBlock(w).findAll('input[type="radio"]')[1]!.setValue(true);
    await engineBtn(factsBlock(w), 'claude').trigger('click');
    expect(val(factsBlock(w).get('.engine-model'))).toBe('haiku');
    expect(factsBlock(w).findAll('.engine-model option').map((o) => o.attributes('value'))).toEqual(['haiku', 'sonnet']);
    // агенту haiku не предлагается
    await engineBtn(agentBlock(w), 'claude').trigger('click');
    expect(agentBlock(w).findAll('.engine-model option').map((o) => o.attributes('value'))).toEqual(['sonnet', 'opus']);
    expect(defaultChoice(engineStatus({ engines: [claude] }), 'claude', 'facts')).toEqual({
      engine: 'claude', model: 'haiku', effort: null,
    });
    w.unmount();
  });

  it('факты: вернуть «как у агента» — PUT /engine/facts с `null`', async () => {
    const w = mountPanel(
      engineStatus({ facts: { engine: 'cursor', model: 'grok-4.7', effort: 'high', follows_agent: false } }),
    );
    await flushPromises();
    expect(chk(factsBlock(w).findAll('input[type="radio"]')[1]!)).toBe(true);
    const put = vi.spyOn(api, 'putEngineFacts').mockResolvedValue(engineStatus());
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await factsBlock(w).findAll('input[type="radio"]')[0]!.setValue(true);
    await factsBlock(w).get('.engine-apply').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith(null);
    expect(w.get('.engine-notice').text()).toBe('Факты: как у агента.');
    expect(chk(factsBlock(w).findAll('input[type="radio"]')[0]!)).toBe(true);
    w.unmount();
  });

  it('старое ядро без блока `facts` — факты «как у агента», панель работает', async () => {
    const status = engineStatus();
    delete status.facts;
    const w = mountPanel(status);
    await flushPromises();
    expect(chk(factsBlock(w).findAll('input[type="radio"]')[0]!)).toBe(true);
    w.unmount();
  });

  it('409 ядра — текст причины в панели, состояние перечитано', async () => {
    const load = vi.spyOn(api, 'getEngine').mockResolvedValue(engineStatus());
    vi.spyOn(store, 'refreshStatus').mockResolvedValue(undefined);
    const w = mount(EnginePanel, { attachTo: document.body });
    await flushPromises();
    vi.spyOn(api, 'putEngine').mockRejectedValue(new api.ApiError(409, 'сначала hub engine setup', null));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await engineBtn(agentBlock(w), 'claude').trigger('click');
    await agentBlock(w).get('.engine-apply').trigger('click');
    await flushPromises();
    expect(w.get('.engine-notice').text()).toBe('сначала hub engine setup');
    expect(load).toHaveBeenCalledTimes(2);
    w.unmount();
  });

  it('удалённая сессия (телефон): ничего не заблокировано, выбор уходит в `PUT /engine`', async () => {
    store.state.remoteSession = true;
    const w = mountPanel();
    await flushPromises();
    expect(engineBtn(agentBlock(w), 'claude').attributes('disabled')).toBeUndefined();
    const put = vi.spyOn(api, 'putEngine').mockResolvedValue({ ...engineStatus(), old_engine: 'opencode' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await engineBtn(agentBlock(w), 'claude').trigger('click');
    await agentBlock(w).get('.engine-apply').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith('claude', { model: 'sonnet', effort: null });
    w.unmount();
  });

  it('403 старого ядра — текст ошибки', async () => {
    const w = mountPanel();
    await flushPromises();
    vi.spyOn(api, 'putEngine').mockRejectedValue(new api.ApiError(403, 'forbidden: local only', null));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await engineBtn(agentBlock(w), 'claude').trigger('click');
    await agentBlock(w).get('.engine-apply').trigger('click');
    await flushPromises();
    expect(w.get('.engine-notice').text()).toBe(PHONE_HINT);
    w.unmount();
  });

  it('Esc закрывает панель', async () => {
    const w = mountPanel();
    await flushPromises();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await flushPromises();
    expect(w.emitted('close')).toBeTruthy();
    w.unmount();
  });

  it('`GET /engine` не ответил — строка ошибки, панель не падает', async () => {
    vi.spyOn(api, 'getEngine').mockRejectedValue(new api.NetworkError(new Error('offline')));
    const w = mount(EnginePanel, { attachTo: document.body });
    await flushPromises();
    expect(w.get('.engine-error').text()).toBe('Не удалось загрузить состояние движка.');
    w.unmount();
  });
});

// --- 3. чистые функции --------------------------------------------------------------------------------------

describe('Ф78-2c: util/engine', () => {
  it('доступность и причина — из данных движка', () => {
    const s = engineStatus();
    expect(optionDisabled(null, 'claude')).toBe(true);
    expect(optionDisabled(s, 'claude')).toBe(false);
    expect(optionDisabled(s, 'codex')).toBe(true);
    expect(optionReason(s, 'codex')).toBe('codex: нет входа — войди в терминале');
    expect(optionReason(s, 'claude')).toBeNull();
    expect(optionReason(engineStatus({ engines: [info('opencode', { available: false, reason: null })] }), 'opencode')).toBe(
      'недоступен',
    );
  });

  it('усилия — модели, иначе движка; смена модели подгоняет усилие', () => {
    const s = engineStatus();
    const claude = s.engines.find((e) => e.name === 'claude')!;
    expect(effortsFor(claude, 'opus')).toEqual(['high', 'xhigh']);
    expect(effortsFor(claude, 'неизвестная')).toEqual(['low', 'medium', 'high']);
    expect(afterModel(s, { engine: 'claude', model: 'sonnet', effort: 'low' }, 'opus')).toEqual({
      engine: 'claude', model: 'opus', effort: null,
    });
    expect(defaultChoice(s, 'claude')).toEqual({ engine: 'claude', model: 'sonnet', effort: null });
    expect(defaultChoice(s, 'cursor')).toEqual({ engine: 'cursor', model: 'grok-4.7', effort: 'high' });
  });

  it('факты: блок ядра или «как у агента» без него', () => {
    expect(factsChoice(engineStatus())).toMatchObject({ follows: true, engine: 'opencode' });
    const old = engineStatus();
    delete old.facts;
    expect(factsChoice(old)).toMatchObject({ follows: true, engine: 'opencode', model: 'deepseek/deepseek-flash' });
    expect(factsChoice(null)).toBeNull();
  });

  it('тексты подтверждения и итога', () => {
    const c = { engine: 'codex' as const, model: 'gpt-6-luna', effort: 'low' };
    expect(choiceLabel(c)).toBe('Codex · gpt-6-luna · low');
    expect(confirmAgentText(c, false)).not.toContain('Факты');
    expect(confirmAgentText(c, true)).toContain('Факты о людях идут за агентом');
    expect(confirmFactsText(null)).toBe('Факты о людях снова пойдут за агентом.');
    expect(switchedNotice(c, 'agent')).toBe('Агент: Codex · gpt-6-luna · low.');
    expect(switchedNotice(null, 'facts')).toBe('Факты: как у агента.');
    expect(switchError(new api.ApiError(409, NOT_READY, null))).toBe(NOT_READY);
    expect(switchError(new api.ApiError(422, 'модель не из списка', null))).toBe('модель не из списка');
    expect(switchError(new api.ApiError(403, 'forbidden', null))).toBe(PHONE_HINT);
    expect(switchError(new api.NetworkError(new Error('x')))).toBe('Не удалось переключить — попробуй ещё раз.');
    expect(ENGINE_LABEL.opencode).toBe('OpenCode');
  });
});
