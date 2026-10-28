// Панель терминала: чанк с xterm не загрузился — автоповтор загрузчика, строка «не загрузился», кнопка «повторить».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { defineComponent, h } from 'vue';

import TerminalHost from '../src/components/TerminalHost.vue';
import { resetTerminal, termState } from '../src/store/terminal';
import { paneLoader } from '../src/util/paneLoader';

const state = { fails: 0, loads: 0 };
const ok = defineComponent({ render: () => h('div', { 'data-test': 'pane-ok' }) });
const realLoad = paneLoader.load;

beforeEach(() => {
  state.loads = 0;
  paneLoader.load = async () => {
    state.loads += 1;
    if (state.loads <= state.fails) throw new Error('chunk failed');
    return { __esModule: true, default: ok };
  };
});

afterEach(() => {
  paneLoader.load = realLoad;
  resetTerminal();
});

function openPanel(): void {
  termState.open = true;
  termState.terms = [{ id: 1, title: '', command: 'bash', owner: 'ui' } as never];
  termState.active = 1;
}

describe('TerminalHost: ленивая загрузка панели', () => {
  it('загрузчик отклоняется один раз — автоповтор, панель появляется', async () => {
    state.fails = 1;
    openPanel();
    const w = mount(TerminalHost, { attachTo: document.body });
    for (let i = 0; i < 20 && !w.find('[data-test="pane-ok"]').exists(); i += 1) await flushPromises();
    expect(state.loads).toBe(2);
    expect(w.find('[data-test="pane-ok"]').exists()).toBe(true);
    w.unmount();
  });

  it('три отказа подряд — строка «не загрузился» и «повторить», повтор грузит панель', async () => {
    state.fails = 3;
    openPanel();
    const w = mount(TerminalHost, { attachTo: document.body });
    for (let i = 0; i < 30 && !w.find('[data-test="term-pane-error"]').exists(); i += 1) await flushPromises();
    expect(w.get('[data-test="term-pane-error"]').text()).toContain('терминал не загрузился');
    await w.get('[data-test="term-pane-retry"]').trigger('click');
    for (let i = 0; i < 30 && !w.find('[data-test="pane-ok"]').exists(); i += 1) await flushPromises();
    expect(w.find('[data-test="pane-ok"]').exists()).toBe(true);
    w.unmount();
  });
});
