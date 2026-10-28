// Ф10-1 A: Ctrl+Enter (Cmd+Enter на Mac) в поле ввода нажимает ГЛАВНУЮ кнопку этого поля — ровно как клик:
// `button.click()` у `disabled`-кнопки ничего не делает, submit-кнопка отправляет форму своим обработчиком.
// Enter и Shift+Enter — перенос строки (не трогаем). Точное сочетание: без Shift/Alt, не зажатое (`repeat`),
// не во время набора IME (`isComposing`). `preventDefault` — только когда сочетание сработало.

interface NavigatorUAData {
  platform?: string;
}

function platformName(): string {
  if (typeof navigator === 'undefined') return '';
  const uaData = (navigator as Navigator & { userAgentData?: NavigatorUAData }).userAgentData;
  return uaData?.platform ?? navigator.platform ?? '';
}

export function isMacPlatform(): boolean {
  return /mac|iphone|ipad|ipod/i.test(platformName());
}

/** Подпись сочетания для `title` главной кнопки и мелкой метки рядом. */
export function submitKeyLabel(): string {
  return isMacPlatform() ? '⌘+Enter' : 'Ctrl+Enter';
}

/** Ф11-4 И5б: сенсорный экран без клавиатуры (телефон) — подсказку Ctrl+Enter не показываем. Браузер не всегда
 * сообщает `pointer: coarse`, поэтому телефон узнаём по любому из трёх признаков: `(pointer: coarse)`,
 * `(hover: none)`, `navigator.maxTouchPoints > 0`. Общее правило для всех полей с подсказкой. */
export function isTouchScreen(): boolean {
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    if (window.matchMedia('(pointer: coarse)').matches || window.matchMedia('(hover: none)').matches) return true;
  }
  return typeof navigator !== 'undefined' && (navigator.maxTouchPoints ?? 0) > 0;
}

/** Показывать ли мелкую метку сочетания рядом с главной кнопкой (на сенсорном экране Ctrl нет). */
export function showSubmitHint(): boolean {
  return !isTouchScreen();
}

/** Ровно Ctrl+Enter или Cmd+Enter (одно из двух), без Shift/Alt, не повтор и не набор IME. */
export function isSubmitCombo(event: KeyboardEvent): boolean {
  if (event.key !== 'Enter' || event.isComposing || event.repeat) return false;
  if (event.shiftKey || event.altKey) return false;
  return event.ctrlKey !== event.metaKey;
}

/** Обработчик `@keydown` поля: при сочетании — клик по главной кнопке, если она есть и доступна. */
export function onSubmitKey(event: KeyboardEvent, button: HTMLButtonElement | null | undefined): void {
  if (!isSubmitCombo(event)) return;
  if (button === null || button === undefined || button.disabled || button.matches(':disabled')) return;
  event.preventDefault();
  button.click();
}
