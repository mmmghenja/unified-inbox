// Ф80-1 (`docs/architecture.md`): починка интерфейса по аудиту двух ширин. B1 — правые панели закрываются
// Esc и нажатием мимо; C1 — шапка окна сессии на телефоне не выпихивает «⋯» (правила стилей); C2 — формы сессии окном
// в столбце на компьютере; D1 — действия с беседой без ленты (`actionsOnly`); F1 — поиск по вводу с задержкой.
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';

import * as api from '../src/api/client';
import type { Channel } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import EnginePanel from '../src/components/EnginePanel.vue';
import FormFrame from '../src/components/FormFrame.vue';
import MachinesPanel from '../src/components/MachinesPanel.vue';
import SearchBox from '../src/components/SearchBox.vue';
import { store } from '../src/store/instance';
import { closeAllPopups } from '../src/util/popup';

enableAutoUnmount(afterEach);

async function settle(): Promise<void> {
  for (let i = 0; i < 4; i++) await nextTick();
}

function pointerDown(el: Element): void {
  el.dispatchEvent(new Event('pointerdown', { bubbles: true }));
}

beforeEach(() => {
  api.setCsrf('c');
  vi.spyOn(api, 'getEngine').mockResolvedValue({
    engine: 'claude', changed_ts: null, engines: ['claude', 'opencode'], opencode_available: false,
    opencode_reason: null, roles: {},
  } as never);
  vi.spyOn(api, 'listMachines').mockResolvedValue({ items: [] } as never);
});

afterEach(() => {
  closeAllPopups();
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('B1: правые панели «⋯» закрываются Esc и нажатием мимо', () => {
  it.each([
    ['EnginePanel', EnginePanel],
    ['MachinesPanel', MachinesPanel],
  ])('%s: Esc -> close', async (_name, component) => {
    const outside = document.createElement('p');
    document.body.appendChild(outside);
    const w = mount(component, { attachTo: document.body });
    await flushPromises();
    await settle();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await settle();
    expect(w.emitted('close')).toHaveLength(1);
  });

  it('нажатие вне панели закрывает, внутри — нет, по терминалу — нет', async () => {
    const outside = document.createElement('p');
    document.body.appendChild(outside);
    const term = document.createElement('section');
    term.className = 'term-panel';
    document.body.appendChild(term);
    const w = mount(MachinesPanel, { attachTo: document.body });
    await flushPromises();
    await settle();
    pointerDown(w.get('.access-panel-header').element);
    pointerDown(term);
    await settle();
    expect(w.emitted('close')).toBeUndefined();
    pointerDown(outside);
    await settle();
    expect(w.emitted('close')).toHaveLength(1);
  });

  it('шапка панели закреплена (sticky), панель кончается над терминалом (--term-h)', () => {
    const css = readFileSync('src/styles/shell.css', 'utf8');
    const header = css.slice(css.indexOf('.access-panel-header {'));
    expect(header.slice(0, header.indexOf('}'))).toContain('position: sticky');
    const panel = css.slice(css.indexOf('.access-panel {'));
    expect(panel.slice(0, panel.indexOf('}'))).toContain('var(--term-h');
  });
});

describe('C1: шапка окна сессии на телефоне', () => {
  it('«⋯» не сжимается и шапка переносится, чипы и заголовок сжимаются с многоточием', () => {
    const css = readFileSync('src/styles/board-view.css', 'utf8');
    const rule = (selector: string): string => {
      const at = css.lastIndexOf(`${selector} {`);
      expect(at).toBeGreaterThan(-1);
      return css.slice(at, css.indexOf('}', at));
    };
    expect(rule('.phone-session-head')).toContain('flex-wrap: wrap');
    expect(rule('.phone-session-head .phone-session-menu-button')).toContain('flex: 0 0 auto');
    expect(rule('.phone-session-title')).toContain('-webkit-line-clamp: 2');
    expect(rule('.phone-session-project')).toContain('text-overflow: ellipsis');
    expect(rule('.phone-session-head .machine-chip,\n.phone-session-head .session-model-chip,\n.phone-session-head .session-bg-chip'))
      .toContain('max-width');
  });
});

describe('C2: рамка формы сессии', () => {
  it('modal — окном поверх в body (Esc и × закрывают), иначе — на месте', async () => {
    const inline = mount(FormFrame, { props: { modal: false, title: 'Роль' }, slots: { default: '<i class="x">форма</i>' } });
    expect(inline.find('.x').exists()).toBe(true);
    expect(document.body.querySelector('.form-frame-modal')).toBeNull();
    inline.unmount();

    const w = mount(FormFrame, { props: { modal: true, title: 'Роль' }, slots: { default: '<i class="x">форма</i>' } });
    expect(document.body.querySelector('.form-frame-modal .x')).not.toBeNull();
    expect(document.body.querySelector('.form-frame-modal')?.getAttribute('aria-label')).toBe('Роль');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(w.emitted('close')).toHaveLength(1);
    (document.body.querySelector('.channel-modal-close') as HTMLButtonElement).click();
    expect(w.emitted('close')).toHaveLength(2);
  });
});

describe('D1: действия с беседой без ленты и поля ввода', () => {
  function channel(): Channel {
    return {
      id: 7, ext_id: 'c7', kind: 'group', title: 'общий', tier: null, muted: false, archived: false, map_reason: null,
      last_msg_ts: 1000, my_msg_count: 0, first_seen_ts: null, unread: 0, project_id: 1, source: 'slack', account_id: 2,
    } as Channel;
  }

  beforeEach(() => {
    store.state.sync.channelsById = { 7: channel() };
    store.state.projects = [{ id: 1, name: 'Globex', color: null, sort: 0, muted: false, workdir: null, unread: 0 }];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([]), { status: 200, headers: { 'content-type': 'application/json' } })));
  });

  it('actionsOnly: проект, стадия, режим, «Напомнить…», «Заглушить», «Удалить…» есть; ленты и поля ввода нет', async () => {
    const w = mount(ChannelView, { props: { channelId: 7, actionsOnly: true }, attachTo: document.body });
    await flushPromises();
    expect(w.find('.project-select').exists()).toBe(true);
    expect(w.find('.stage-control').exists()).toBe(true);
    expect(w.find('.attention-select').exists()).toBe(true);
    expect(w.find('.remind-button').exists()).toBe(true);
    expect(w.find('.mute-button').exists()).toBe(true);
    expect(w.find('.hide-channel-button').exists()).toBe(true);
    expect(w.find('.message-feed').exists()).toBe(false);
    expect(w.find('.composer').exists()).toBe(false);
    expect(w.find('h2').exists()).toBe(false);
  });
});

describe('F1: поиск по вводу с задержкой', () => {
  it('без Enter через ~300 мс ищет, «ничего не найдено» и ошибка сети — текстом', async () => {
    vi.useFakeTimers();
    const search = vi.spyOn(api, 'search').mockResolvedValue([]);
    const w = mount(SearchBox, { attachTo: document.body });
    await w.get('input').setValue('до');
    vi.advanceTimersByTime(299);
    expect(search).not.toHaveBeenCalled();
    vi.advanceTimersByTime(2);
    await flushPromises();
    expect(search).toHaveBeenCalledWith('до');
    expect(w.text()).toContain('Ничего не найдено');

    search.mockRejectedValue(new api.NetworkError(new Error('offline')));
    await w.get('input').setValue('договор');
    vi.advanceTimersByTime(301);
    await flushPromises();
    expect(w.get('[role="alert"]').text()).toContain('Не удалось выполнить поиск');
  });

  it('результат: название и текст в отдельных блоках', async () => {
    vi.useFakeTimers();
    vi.spyOn(api, 'search').mockResolvedValue([
      { message_id: 1, channel_id: 2, channel_title: 'Иван Петров', text: 'Привет!', ts: 1 } as never,
    ]);
    const w = mount(SearchBox, { attachTo: document.body });
    await w.get('input').setValue('при');
    vi.advanceTimersByTime(301);
    await flushPromises();
    expect(w.get('.search-result-title').text()).toBe('Иван Петров');
    expect(w.get('.search-result-text').text()).toBe('Привет!');
  });
});
