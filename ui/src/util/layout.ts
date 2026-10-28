// Ф10-6 B1 (`docs/architecture.md`: режим раскладки — один на приложение, из двух
// `matchMedia` с подпиской на `change`. `full` — весь экран (≥ 1280 px), `phone` — телефон (< 600 px), иначе
// `half` (пол-экрана). CSS (`styles/shell.css`) использует те же запросы в синтаксисе диапазонов — между
// режимами нет дыры (599.5 px — ещё телефон, 600 — уже пол-экрана). `matchMedia` нет (jsdom) — `full`.
import { reactive } from 'vue';

export type LayoutMode = 'full' | 'half' | 'phone';

export const FULL_QUERY = '(width >= 1280px)';
export const PHONE_QUERY = '(width < 600px)';

export type MatchMediaFn = (query: string) => MediaQueryList;

export const layout = reactive<{ mode: LayoutMode }>({ mode: 'full' });

let fullList: MediaQueryList | null = null;
let phoneList: MediaQueryList | null = null;

function recompute(): void {
  if (fullList === null || phoneList === null) {
    layout.mode = 'full';
    return;
  }
  if (phoneList.matches) layout.mode = 'phone';
  else if (fullList.matches) layout.mode = 'full';
  else layout.mode = 'half';
}

function unsubscribe(): void {
  fullList?.removeEventListener?.('change', recompute);
  phoneList?.removeEventListener?.('change', recompute);
  fullList = null;
  phoneList = null;
}

function defaultMatchMedia(): MatchMediaFn | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null;
  return window.matchMedia.bind(window);
}

/** Подписаться на `matchMedia` и пересчитать режим. `null` — `matchMedia` нет: режим `full`. Тесты подменяют
 * `matchMedia` своим и зовут снова; повторный вызов снимает прежнюю подписку. */
export function initLayout(matchMedia: MatchMediaFn | null = defaultMatchMedia()): void {
  unsubscribe();
  if (matchMedia !== null) {
    try {
      fullList = matchMedia(FULL_QUERY);
      phoneList = matchMedia(PHONE_QUERY);
      fullList.addEventListener?.('change', recompute);
      phoneList.addEventListener?.('change', recompute);
    } catch {
      unsubscribe();
    }
  }
  recompute();
}

export function isPhone(): boolean {
  return layout.mode === 'phone';
}

initLayout();
