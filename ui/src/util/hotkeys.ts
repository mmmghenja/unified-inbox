// Ф99-5: общий реестр горячих клавиш (`docs/architecture.md` и поправка П9). Один обработчик на
// `document`; записи лежат стеком — верхняя (последняя зарегистрированная: модалка, меню, окно сессии) проверяется
// первой, `modal: true` закрывает от неё нижние. Сравнение — по `event.code` (русская раскладка не мешает).
// Правила: внутри `.xterm` реестр молчит целиком; одиночные клавиши (без Alt/Ctrl) — только вне полей ввода;
// сочетания с Alt/Ctrl — и в полях. На телефоне (узкая ширина или грубый указатель) реестр не включается.
import { reactive } from 'vue';

import { layout } from './layout';

export interface Hotkey {
  /** `event.code`: `KeyJ`, `Digit1`, `Slash`, `ArrowLeft`, `Period`… */
  code: string;
  alt?: boolean;
  ctrl?: boolean;
  shift?: boolean;
  /** Подпись для шпаргалки: `Alt+N`, `?`. */
  label: string;
  /** Что делает (для шпаргалки). */
  desc: string;
  /** Где действует (для шпаргалки): «везде», «окно сессии»… */
  where: string;
  /** Вернуть `false` — клавиша не обработана (идёт к нижним записям); иначе считается съеденной. */
  run: (event: KeyboardEvent) => boolean | void;
  /** Дополнительное условие (элемент в фокусе, нужный экран). */
  when?: () => boolean;
  /** Отменять действие браузера (по умолчанию да). */
  preventDefault?: boolean;
}

export interface HotkeyEntry {
  keys: Hotkey[];
  /** Модалка: нижние записи при этом молчат. */
  modal?: boolean;
}

const stack: HotkeyEntry[] = [];
let installed = false;

export const hotkeyHelp = reactive({ open: false });

/** Счётчики запросов к компонентам, у которых состояние локальное (`TopMenu`: «Новое сообщение», поиск). */
export const hotkeyBus = reactive({ newMessage: 0, search: 0 });

/** Реестр выключен на телефоне: узкая раскладка или грубый указатель (палец). */
export function hotkeysEnabled(): boolean {
  if (layout.mode === 'phone') return false;
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    try {
      if (window.matchMedia('(pointer: coarse)').matches) return false;
    } catch {
      /* нет — считаем мышью */
    }
  }
  return true;
}

function inTerminal(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest('.xterm') !== null;
}

export function inField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
}

function matches(key: Hotkey, event: KeyboardEvent): boolean {
  if (event.code !== key.code) return false;
  if (event.metaKey) return false;
  if (event.altKey !== (key.alt === true)) return false;
  if (event.ctrlKey !== (key.ctrl === true)) return false;
  if (event.shiftKey !== (key.shift === true)) return false;
  return true;
}

function onKeydown(event: KeyboardEvent): void {
  if (event.defaultPrevented || event.isComposing) return;
  if (stack.length === 0 || !hotkeysEnabled()) return;
  if (inTerminal(event.target)) return;
  const single = !event.altKey && !event.ctrlKey;
  if (single && inField(event.target)) return;
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    const entry = stack[i]!;
    for (const key of entry.keys) {
      if (!matches(key, event)) continue;
      if (key.when !== undefined && !key.when()) continue;
      const result = key.run(event);
      if (result === false) continue;
      if (key.preventDefault !== false) event.preventDefault();
      return;
    }
    if (entry.modal === true) return;
  }
}

function install(): void {
  if (installed) return;
  installed = true;
  document.addEventListener('keydown', onKeydown);
}

function uninstall(): void {
  if (!installed || stack.length > 0) return;
  installed = false;
  document.removeEventListener('keydown', onKeydown);
}

/** Зарегистрировать записи на верх стека; вернуть функцию снятия. */
export function registerHotkeys(entry: HotkeyEntry): () => void {
  stack.push(entry);
  install();
  return () => {
    const at = stack.indexOf(entry);
    if (at >= 0) stack.splice(at, 1);
    uninstall();
  };
}

/** Все записи стека для шпаргалки (верхние первыми; одинаковые подписи не дублируются). */
export function listHotkeys(): Hotkey[] {
  const seen = new Set<string>();
  const out: Hotkey[] = [];
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    for (const key of stack[i]!.keys) {
      const id = `${key.label}|${key.where}`;
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(key);
    }
  }
  return out;
}

/** Сколько записей в стеке (для тестов). */
export function hotkeyStackSize(): number {
  return stack.length;
}
