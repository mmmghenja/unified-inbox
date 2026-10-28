// Ф99-5: реестр горячих клавиш — стек, event.code, молчание в терминале и полях, телефон.
import { afterEach, describe, expect, it, vi } from 'vitest';

import { hotkeyStackSize, hotkeysEnabled, registerHotkeys, listHotkeys, type Hotkey } from '../src/util/hotkeys';
import { layout } from '../src/util/layout';

function key(code: string, extra: Partial<Hotkey> = {}, run: () => boolean | void = () => undefined): Hotkey {
  return { code, label: code, desc: code, where: 'тест', run, ...extra };
}

function press(target: EventTarget, init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

const releases: Array<() => void> = [];
function reg(entry: Parameters<typeof registerHotkeys>[0]): void {
  releases.push(registerHotkeys(entry));
}

afterEach(() => {
  while (releases.length > 0) releases.pop()!();
  layout.mode = 'full';
  document.body.innerHTML = '';
  expect(hotkeyStackSize()).toBe(0);
});

describe('реестр горячих клавиш', () => {
  it('сравнивает по event.code: русская раскладка (key = «о») срабатывает как J', () => {
    const run = vi.fn();
    reg({ keys: [key('KeyJ', {}, run)] });
    press(document.body, { code: 'KeyJ', key: 'о' });
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('модификаторы строгие: Alt+N не срабатывает на N и наоборот', () => {
    const alt = vi.fn();
    const plain = vi.fn();
    reg({ keys: [key('KeyN', { alt: true }, alt), key('KeyM', {}, plain)] });
    press(document.body, { code: 'KeyN' });
    press(document.body, { code: 'KeyM', altKey: true });
    expect(alt).not.toHaveBeenCalled();
    expect(plain).not.toHaveBeenCalled();
    press(document.body, { code: 'KeyN', altKey: true });
    expect(alt).toHaveBeenCalledTimes(1);
  });

  it('стек: верхняя запись первой; модалка закрывает нижние; false пропускает вниз', () => {
    const low = vi.fn();
    const top = vi.fn();
    reg({ keys: [key('KeyJ', {}, low), key('KeyK', {}, low)] });
    reg({ keys: [key('KeyJ', {}, top)] });
    press(document.body, { code: 'KeyJ' });
    expect(top).toHaveBeenCalledTimes(1);
    expect(low).not.toHaveBeenCalled();
    // KeyK у верхней нет — идёт вниз.
    press(document.body, { code: 'KeyK' });
    expect(low).toHaveBeenCalledTimes(1);
    // верхняя вернула false — тоже вниз
    reg({ keys: [key('KeyK', {}, () => false)] });
    press(document.body, { code: 'KeyK' });
    expect(low).toHaveBeenCalledTimes(2);
    // модалка гасит нижних
    reg({ modal: true, keys: [] });
    press(document.body, { code: 'KeyK' });
    expect(low).toHaveBeenCalledTimes(2);
  });

  it('одиночные клавиши молчат в полях ввода, сочетания с Alt — нет', () => {
    const single = vi.fn();
    const alt = vi.fn();
    reg({ keys: [key('KeyJ', {}, single), key('KeyS', { alt: true }, alt)] });
    const input = document.createElement('textarea');
    document.body.append(input);
    press(input, { code: 'KeyJ' });
    expect(single).not.toHaveBeenCalled();
    press(input, { code: 'KeyS', altKey: true });
    expect(alt).toHaveBeenCalledTimes(1);
  });

  it('в .xterm молчит целиком, даже Alt-сочетания', () => {
    const single = vi.fn();
    const alt = vi.fn();
    reg({ keys: [key('KeyJ', {}, single), key('KeyS', { alt: true }, alt)] });
    const term = document.createElement('div');
    term.className = 'xterm';
    const inner = document.createElement('textarea');
    term.append(inner);
    document.body.append(term);
    press(inner, { code: 'KeyJ' });
    press(inner, { code: 'KeyS', altKey: true });
    expect(single).not.toHaveBeenCalled();
    expect(alt).not.toHaveBeenCalled();
  });

  it('preventDefault по умолчанию (Alt+←/→ — «назад» в окне приложения), можно отключить', () => {
    reg({ keys: [key('ArrowLeft', { alt: true }), key('ArrowRight', { alt: true, preventDefault: false })] });
    expect(press(document.body, { code: 'ArrowLeft', altKey: true }).defaultPrevented).toBe(true);
    expect(press(document.body, { code: 'ArrowRight', altKey: true }).defaultPrevented).toBe(false);
  });

  it('на телефоне реестр не включается', () => {
    const run = vi.fn();
    reg({ keys: [key('KeyJ', {}, run)] });
    layout.mode = 'phone';
    expect(hotkeysEnabled()).toBe(false);
    press(document.body, { code: 'KeyJ' });
    expect(run).not.toHaveBeenCalled();
  });

  it('шпаргалка берёт список из реестра без дублей', () => {
    reg({ keys: [key('KeyJ', { label: 'J', where: 'a' })] });
    reg({ keys: [key('KeyJ', { label: 'J', where: 'a' }), key('KeyK', { label: 'K', where: 'a' })] });
    expect(listHotkeys().map((k) => k.label).sort()).toEqual(['J', 'K']);
  });
});
