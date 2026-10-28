// Ф80-2 (`docs/architecture.md`): хвосты починки интерфейса. п.1 — «Ждёт» режется целыми строками
// (line-clamp), время и счётчик nowrap; п.2 — карточки разрешений в окне сессии ограничены по высоте; п.3 — номер беседы
// ужимается первым; п.4 — кнопки сообщения не занимают место; п.7 — встроенная форма сессии закрывается Esc и «Закрыть».
import { enableAutoUnmount, mount } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it } from 'vitest';

import FormFrame from '../src/components/FormFrame.vue';

enableAutoUnmount(afterEach);

const shell = readFileSync('src/styles/shell.css', 'utf8');
const layout = readFileSync('src/styles/layout.css', 'utf8');

function rule(css: string, selector: string): string {
  const at = css.lastIndexOf(`${selector} {`);
  expect(at, selector).toBeGreaterThanOrEqual(0);
  return css.slice(at, css.indexOf('}', at));
}

describe('п.1: «Ждёт» — целые строки, время не рвётся', () => {
  it('заголовок, последняя строка и текст черновика — line-clamp 2; время и счётчик — nowrap', () => {
    for (const sel of ['.wait-row-title', '.wait-row-last', '.wait-item .draft-row-who', '.wait-item .draft-row-text']) {
      const r = rule(shell, sel);
      expect(r, sel).toContain('-webkit-line-clamp: 2');
      expect(r, sel).toContain('white-space: normal');
    }
    expect(rule(shell, '.wait-row-head .rule-mark')).toContain('white-space: nowrap');
  });
});

describe('п.2: карточки разрешений в окне сессии', () => {
  it('блок не сжимается до нуля и не выше 12rem / 35vh, дальше прокрутка', () => {
    const r = rule(shell, '.session-requests');
    expect(r).toContain('flex: none');
    expect(r).toContain('max-height: min(12rem, 35vh)');
    expect(r).toContain('overflow-y: auto');
  });

  it('SessionWindow кладёт карточки в .session-requests, в столбце — строкой', () => {
    const src = readFileSync('src/components/SessionWindow.vue', 'utf8');
    expect(src).toContain('class="session-requests"');
    expect(src).toContain(':one-line="item.stale ? undefined : column === true"');
  });
});

describe('п.3: номер беседы ужимается первым', () => {
  it('у номера вес сжатия выше, чем у названия', () => {
    expect(rule(shell, '.channel-button .chan-no')).toContain('flex: 0 100 auto');
    expect(rule(shell, '.channel-button .channel-title')).toContain('flex: 1 0 auto');
  });
});

describe('п.4: кнопки сообщения не занимают строку', () => {
  it('в узкой ленте на устройствах с наведением группа кнопок лежит поверх угла строки', () => {
    expect(layout).toContain('container-type: inline-size');
    expect(layout).toContain('@container (max-width: 30rem)');
    expect(layout).toContain('position: absolute');
    const src = readFileSync('src/components/MessageRow.vue', 'utf8');
    expect(src).toContain('class="message-actions"');
  });
});

describe('п.7: встроенная форма сессии (телефон)', () => {
  it('Esc закрывает', () => {
    const w = mount(FormFrame, { props: { modal: false, title: 'Роль сессии' }, slots: { default: '<i class="x">форма</i>' } });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(w.emitted('close')).toHaveLength(1);
  });

  it('в шапке — название и «Закрыть»; форма на месте', async () => {
    const w = mount(FormFrame, { props: { modal: false, title: 'Новая сессия' }, slots: { default: '<i class="x">форма</i>' } });
    expect(w.find('.x').exists()).toBe(true);
    expect(w.find('.form-frame-inline-head').text()).toContain('Новая сессия');
    await w.find('.form-frame-inline-close').trigger('click');
    expect(w.emitted('close')).toHaveLength(1);
  });

  it('форма «Новая» в «Сессиях» обёрнута в FormFrame', () => {
    const src = readFileSync('src/components/PhoneSessions.vue', 'utf8');
    expect(src).toMatch(/<FormFrame[\s\S]*title="Новая сессия"[\s\S]*<NewSessionForm/);
  });
});

describe('п.6: «Автопилот» на телефоне — первый столбец закреплён', () => {
  it('sticky left: 0 с фоном', () => {
    const css = readFileSync('src/styles/board-view.css', 'utf8');
    const at = css.indexOf('@media (width < 600px)', css.indexOf('.autopilot-table .autopilot-first'));
    const block = css.slice(at, css.indexOf('.autopilot-table {', at));
    expect(block).toContain('position: sticky');
    expect(block).toContain('left: 0');
    expect(block).toContain('background: var(--bg)');
  });
});
