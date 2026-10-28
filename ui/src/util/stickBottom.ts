// Ф23-1: «держаться низа ленты, пока владелец сам не прокрутил вверх». Общая механика для ленты беседы и панели
// треда. Высота содержимого растёт после отрисовки (картинки, письма, разметка) — `ResizeObserver` на
// содержимом прокручивает вниз, пока `stick` истинно. `stick` снимает ТОЛЬКО прокрутка владельца (событие scroll
// дальше `NEAR_BOTTOM_PX` от низа); наша собственная прокрутка (к низу, компенсация догрузки, к цели фокуса)
// помечена флагом на короткое окно и `stick` не меняет.

export const NEAR_BOTTOM_PX = 80;
const PROGRAMMATIC_WINDOW_MS = 120;

export interface StickToBottom {
  /** Держим ли низ (читается снаружи: «новое сообщение — доскроллить»). */
  readonly stick: boolean;
  isNearBottom(): boolean;
  /** Наша прокрутка к низу (флаг «сами» на один кадр). */
  scrollToBottom(): void;
  /** Любая другая наша прокрутка (компенсация, `scrollIntoView`) — не снимает и не ставит `stick`. */
  programmatic(fn: () => void): void;
  /** Обработчик события scroll: прокрутка владельца обновляет `stick`. */
  onScroll(): void;
  /** Руками: держать / не держать (смена беседы; цель фокуса — не держать). */
  setStick(value: boolean): void;
  /** Цель фокуса глубже низа: пока владелец не прокрутил сам, рост высоты (картинки выше) возвращает её в центр. */
  anchor(el: HTMLElement | null): void;
  /** Наблюдать высоту содержимого; повторный вызов переключает цель. */
  observe(...contents: (Element | null)[]): void;
  dispose(): void;
}

export function createStickToBottom(getScroller: () => HTMLElement | null): StickToBottom {
  let stick = true;
  let programmaticUntil = 0;
  let observer: ResizeObserver | null = null;
  let anchorEl: HTMLElement | null = null;

  function now(): number {
    return Date.now();
  }

  function markProgrammatic(): void {
    programmaticUntil = now() + PROGRAMMATIC_WINDOW_MS;
  }

  function isNearBottom(): boolean {
    const el = getScroller();
    if (el === null) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
  }

  function scrollToBottom(): void {
    const el = getScroller();
    if (el === null) return;
    markProgrammatic();
    el.scrollTop = el.scrollHeight;
  }

  const api: StickToBottom = {
    get stick() {
      return stick;
    },
    isNearBottom,
    scrollToBottom,
    programmatic(fn) {
      markProgrammatic();
      fn();
    },
    onScroll() {
      if (now() < programmaticUntil) return; // наша прокрутка — позицию владельца не отражает
      stick = isNearBottom();
      anchorEl = null; // владелец прокрутил сам — позицию больше не навязываем
    },
    setStick(value) {
      stick = value;
      if (value) anchorEl = null;
    },
    anchor(el) {
      anchorEl = el;
    },
    observe(...contents) {
      observer?.disconnect();
      observer = null;
      const targets = contents.filter((c): c is Element => c !== null);
      if (targets.length === 0 || typeof ResizeObserver === 'undefined') return;
      observer = new ResizeObserver(() => {
        if (stick) scrollToBottom();
        else if (anchorEl !== null && anchorEl.isConnected) {
          const target = anchorEl;
          markProgrammatic();
          target.scrollIntoView?.({ block: 'center' });
        }
      });
      for (const target of targets) observer.observe(target);
    },
    dispose() {
      observer?.disconnect();
      observer = null;
    },
  };
  return api;
}
