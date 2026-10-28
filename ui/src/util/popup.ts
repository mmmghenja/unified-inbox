// Ф80-1 A: общий механизм всплывающих меню. Раньше каждое меню само решало, когда закрыться (у большинства — только
// повторным кликом по своей кнопке), и они копились одновременно. Теперь: один обработчик на `document`; открытие
// нового меню закрывает прежние (кроме тех, внутри которых оно лежит — вложенные меню); Esc закрывает самое
// верхнее; нажатие вне меню и вне его кнопки закрывает; ограничение высоты и прижим к краям экрана — при открытии.
import { nextTick, onBeforeUnmount, ref, watch, type Ref } from 'vue';

type El = HTMLElement | null | undefined;

interface Entry {
  roots: () => El[];
  close: () => void;
  /** Селектор: нажатие внутри таких элементов не считается «мимо» (терминал, модалки — для правых панелей). */
  ignore?: string;
  /** Нажатие вне не закрывает (встроенный блок-форма: закрывается Esc и открытием другого меню). */
  keepOnOutside?: boolean;
}

const MARGIN = 8;
const MIN_HEIGHT = 120;
const stack: Entry[] = [];
let installed = false;

function contains(entry: Entry, target: Node): boolean {
  if (entry.roots().some((el) => el != null && el.contains(target))) return true;
  return entry.ignore !== undefined && target instanceof Element && target.closest(entry.ignore) !== null;
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || stack.length === 0) return;
  const top = stack[stack.length - 1];
  if (top !== undefined) top.close();
}

function onPointerDown(event: Event): void {
  const target = event.target;
  if (!(target instanceof Node)) return;
  for (const entry of [...stack]) {
    if (entry.keepOnOutside !== true && !contains(entry, target)) entry.close();
  }
}

function install(): void {
  if (installed) return;
  installed = true;
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('pointerdown', onPointerDown, true);
  // Запасные события (в т. ч. `click`, который шлёт `element.click()` и клавиатура): окружения без pointer-событий и ранние тесты (повторное закрытие безвредно).
  document.addEventListener('mousedown', onPointerDown, true);
  document.addEventListener('touchstart', onPointerDown, true);
  document.addEventListener('click', onPointerDown, true);
}

function uninstall(): void {
  if (!installed || stack.length > 0) return;
  installed = false;
  document.removeEventListener('keydown', onKeydown);
  document.removeEventListener('pointerdown', onPointerDown, true);
  document.removeEventListener('mousedown', onPointerDown, true);
  document.removeEventListener('touchstart', onPointerDown, true);
  document.removeEventListener('click', onPointerDown, true);
}

/** Зарегистрировать открытое меню; вернуть функцию снятия. Прежние меню, не содержащие новое, закрываются. */
export function registerPopup(entry: Entry): () => void {
  for (const other of [...stack]) {
    const nested = entry.roots().some((el) => el != null && other.roots().some((o) => o != null && o.contains(el)));
    if (!nested) other.close();
  }
  stack.push(entry);
  install();
  return () => {
    const at = stack.indexOf(entry);
    if (at >= 0) stack.splice(at, 1);
    uninstall();
  };
}

/** Закрыть все открытые меню (смена вкладки и т. п.). */
export function closeAllPopups(): void {
  for (const entry of [...stack]) entry.close();
}

/** Сколько меню открыто (для тестов). */
export function openPopupCount(): number {
  return stack.length;
}

/** Ограничить высоту меню экраном и прижать к краям с отступом 8 px (A3). Можно звать повторно. */
export function placePopup(panel: El): void {
  if (panel == null) return;
  panel.style.transform = '';
  panel.style.maxHeight = '';
  const rect = panel.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.visualViewport?.height ?? window.innerHeight;
  if (vh > 0 && rect.top >= 0) {
    panel.style.maxHeight = `${Math.max(MIN_HEIGHT, Math.floor(vh - rect.top - MARGIN))}px`;
  }
  panel.style.overflowY = 'auto';
  panel.style.overscrollBehavior = 'contain';
  if (vw > 0) {
    let dx = 0;
    if (rect.right > vw - MARGIN) dx = vw - MARGIN - rect.right;
    if (rect.left + dx < MARGIN) dx = MARGIN - rect.left;
    if (dx !== 0) panel.style.transform = `translateX(${Math.round(dx)}px)`;
  }
}

interface Options {
  /** Открыто ли меню. Меню закрывается тем, что мы ставим `false`. */
  open: Ref<boolean>;
  /** Кнопка открытия — нажатие по ней не считается «мимо» (её клик сам переключает меню). */
  button?: () => El;
  /** Само меню (и всё, что считается его частью). */
  panel: () => El;
  /** Ограничивать высоту и прижимать к краям (по умолчанию да). */
  place?: boolean;
  /** Дополнительное действие при закрытии (сброс внутреннего состояния). */
  onClose?: () => void;
  /** Нажатие внутри элементов с таким селектором не считается «мимо». */
  ignore?: string;
  /** Нажатие вне меню его не закрывает (встроенный блок-форма). */
  keepOnOutside?: boolean;
}

/** Подключить меню компонента к общему механизму. */
export function usePopup(options: Options): void {
  let release: (() => void) | null = null;
  const close = (): void => {
    options.open.value = false;
    options.onClose?.();
  };
  const unregister = (): void => {
    if (release !== null) release();
    release = null;
  };
  const onResize = (): void => {
    if (options.place !== false) placePopup(options.panel());
  };
  // Содержимое меню может дорасти после открытия (загрузка списка) — положение пересчитывается.
  let observer: ResizeObserver | null = null;
  const stopObserving = (): void => {
    observer?.disconnect();
    observer = null;
  };
  watch(
    options.open,
    async (isOpen) => {
      unregister();
      stopObserving();
      window.removeEventListener('resize', onResize);
      if (!isOpen) {
        // v-show-меню остаётся в DOM: убираем рассчитанные при открытии высоту и сдвиг.
        const el = options.panel();
        if (el != null && options.place !== false) {
          el.style.maxHeight = '';
          el.style.transform = '';
        }
        return;
      }
      await nextTick();
      if (!options.open.value) return;
      release = registerPopup({
        roots: () => [options.button?.(), options.panel()],
        close,
        ignore: options.ignore,
        keepOnOutside: options.keepOnOutside,
      });
      if (options.place !== false) {
        placePopup(options.panel());
        window.addEventListener('resize', onResize);
        const el = options.panel();
        if (el != null && typeof ResizeObserver !== 'undefined') {
          observer = new ResizeObserver(onResize);
          observer.observe(el);
        }
      }
    },
    { flush: 'post', immediate: true },
  );
  onBeforeUnmount(() => {
    unregister();
    stopObserving();
    window.removeEventListener('resize', onResize);
  });
}

/** Что не считается «мимо» для правых панелей: терминал и модальные окна поверх доски. */
const PANEL_IGNORE =
  '.term-panel, .channel-modal-overlay, .person-overlay, .autopilot-overlay, .draft-modal-overlay, .media-viewer, .new-message-overlay';

/**
 * Ф80-1 B1: правая панель «⋯» (Раскладка, Люди, Машины, Доступ, Движок). Панель смонтирована сразу открытой:
 * закрывается Esc и (на компьютере — это «мимо») нажатием вне панели; закрытие сообщается через `close`.
 */
export function usePanel(panel: () => El, close: () => void, opts: { ignore?: string; keepOnOutside?: boolean } = {}): void {
  const open = ref(true);
  watch(open, (isOpen) => {
    if (!isOpen) close();
  });
  usePopup({
    open,
    panel,
    place: false,
    ignore: opts.ignore === undefined ? PANEL_IGNORE : `${PANEL_IGNORE}, ${opts.ignore}`,
    keepOnOutside: opts.keepOnOutside,
  });
}
