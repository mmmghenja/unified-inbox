// Ф56-5б: склейка с xterm, не зависящая от DOM, — проверяется тестом без настоящего xterm.
import type { Terminal } from '@xterm/xterm';

import type { TermSnapshotFrame } from '../api/types';

/** К9: запросы программы к терминалу — «обработаны» (true), ответа в `onData` не будет: отвечает только tmux, иначе в
 * оболочке оседает мусор вроде `^[[?1;2c`. OSC, меняющие цвет (без `?`), не трогаем. */
export function silenceReplies(t: Pick<Terminal, 'parser'>): void {
  const p = t.parser;
  const swallow = (): boolean => true;
  p.registerCsiHandler({ final: 'c' }, swallow); // DA1
  p.registerCsiHandler({ prefix: '>', final: 'c' }, swallow); // DA2
  p.registerCsiHandler({ prefix: '=', final: 'c' }, swallow); // DA3
  p.registerCsiHandler({ final: 'n' }, swallow); // DSR
  p.registerCsiHandler({ prefix: '?', final: 'n' }, swallow);
  p.registerCsiHandler({ intermediates: '$', final: 'p' }, swallow); // DECRQM
  p.registerCsiHandler({ prefix: '?', intermediates: '$', final: 'p' }, swallow);
  p.registerCsiHandler({ final: 't' }, swallow); // отчёты оконных операций
  p.registerCsiHandler({ prefix: '>', final: 'q' }, swallow); // XTVERSION
  p.registerDcsHandler({ intermediates: '$', final: 'q' }, swallow); // DECRQSS
  for (const ident of [4, 10, 11, 12, 17, 19]) {
    p.registerOscHandler(ident, (data: string) => data.split(';').includes('?'));
  }
}

/** Escape-последовательности режимов вокруг записи снимка (К6): `head` — до данных (alt-screen), `tail` — после
 * (область прокрутки, origin, курсор, режимы клавиш и мыши). В режиме origin строка курсора отсчитывается от верха
 * области прокрутки. */
export function restoreSequences(f: TermSnapshotFrame): { head: string; tail: string } {
  const m = f.modes ?? {};
  const head = f.alt_screen ? '\x1b[?1049h' : '';
  let tail = '';
  const [x, y] = f.cursor;
  const region = m.scroll_region;
  const top = region?.[0] ?? 0;
  if (region !== undefined && !(region[0] === 0 && region[1] >= f.rows - 1)) {
    tail += `\x1b[${region[0] + 1};${region[1] + 1}r`;
  }
  if (m.origin_flag === true) {
    tail += '\x1b[?6h';
    tail += `\x1b[${Math.max(1, y - top + 1)};${x + 1}H`;
  } else {
    tail += `\x1b[${y + 1};${x + 1}H`;
  }
  if (m.keypad_cursor_flag === true) tail += '\x1b[?1h';
  if (m.keypad_flag === true) tail += '\x1b=';
  if (m.cursor_flag === false) tail += '\x1b[?25l';
  if (m.insert_flag === true) tail += '\x1b[4h';
  if (m.wrap_flag === false) tail += '\x1b[?7l';
  if (m.mouse_standard_flag === true) tail += '\x1b[?1000h';
  if (m.mouse_button_flag === true) tail += '\x1b[?1002h';
  if (m.mouse_any_flag === true) tail += '\x1b[?1003h';
  if (m.mouse_sgr_flag === true) tail += '\x1b[?1006h';
  if (m.mouse_utf8_flag === true) tail += '\x1b[?1005h';
  return { head, tail };
}
