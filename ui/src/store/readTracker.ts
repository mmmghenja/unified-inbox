// Ф8-1 B: общая логика «прочитано» ленты канала (`MessageFeed.vue`) и панели треда (`ThreadPanel.vue`) —
// одна на оба места, не копия. Без Vue: решение «показано ли сообщение» — чистая функция (тест без DOM),
// сам трекер держит наблюдатель, множество показанных id, дебаунс и слушатели фокуса/видимости.
//
// Почему не `threshold: 0.6`, как было: сообщение выше 1/0.6 высоты ленты (длинное письмо) никогда не
// достигает доли 0.6 — колбэк при прокрутке к нему не срабатывал, «прочитано» до него не уходило.
import type { Message } from '../api/types';
import { findBottomVisibleMessage, isNewerInDisplayOrder } from './visibleMessage';

/** Доля, начиная с которой сообщение считается показанным: видно ≥ 60 % его высоты ИЛИ оно занимает
 * ≥ 60 % высоты ленты. */
export const SHOWN_FRACTION = 0.6;

/** Пороги наблюдателя — шаг 0.05 (0.6 среди них ровно): колбэк приходит достаточно часто, чтобы решение по
 * доле видимой высоты ленты (длинное письмо) принималось при прокрутке, а не только на краях. */
export const READ_OBSERVER_THRESHOLDS: readonly number[] = Array.from({ length: 21 }, (_, i) => i / 20);

export const MARK_READ_DEBOUNCE_MS = 600;

export interface ShownInput {
  /** `IntersectionObserverEntry.intersectionRatio` — видимая доля высоты самого сообщения. */
  intersectionRatio: number;
  /** Высота видимой части сообщения (`intersectionRect.height`), px. */
  visibleHeight: number;
  /** Высота видимой области ленты (корня наблюдателя), px. */
  rootHeight: number;
}

/** Ф8-1 B1: показано ли сообщение — чистая функция, без DOM. */
export function isMessageShown({ intersectionRatio, visibleHeight, rootHeight }: ShownInput): boolean {
  if (intersectionRatio >= SHOWN_FRACTION) return true;
  return rootHeight > 0 && visibleHeight >= SHOWN_FRACTION * rootHeight;
}

/** Решение по записи наблюдателя: высота корня — из `rootBounds`, иначе из самого корня (`clientHeight`). */
export function isEntryShown(entry: IntersectionObserverEntry, root: HTMLElement | null): boolean {
  if (!entry.isIntersecting) return false;
  const rootHeight = entry.rootBounds?.height ?? root?.clientHeight ?? 0;
  return isMessageShown({
    intersectionRatio: entry.intersectionRatio ?? 0,
    visibleHeight: entry.intersectionRect?.height ?? 0,
    rootHeight,
  });
}

export interface ReadTrackerOptions {
  /** Сообщения в порядке показа (плоский список ленты или ответы треда). */
  messages: () => readonly Message[];
  /** Лента прокручена к низу (тот же критерий `isNearBottom`, что у автопрокрутки). */
  isAtBottom: () => boolean;
  /** Отправить «прочитано» до этого сообщения. Вернуть `false`, если отправить некуда (панель закрыта). */
  markRead: (message: Message) => boolean;
  delayMs?: number;
}

export interface ReadTracker {
  /** (Пере)подключить наблюдатель к строкам `[data-message-id]` корня. */
  attach(root: HTMLElement | null): void;
  /** Запустить (перезапустить) дебаунс отправки «прочитано». */
  schedule(): void;
  /** Новая беседа — забыть, до какого сообщения уже отмечено. */
  reset(): void;
  /** Снять наблюдатель, таймер и слушатели фокуса/видимости (размонтирование). */
  dispose(): void;
}

export function createReadTracker(options: ReadTrackerOptions): ReadTracker {
  const delayMs = options.delayMs ?? MARK_READ_DEBOUNCE_MS;
  let observer: IntersectionObserver | null = null;
  let root: HTMLElement | null = null;
  const visibleIds = new Set<number>();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastMarked: Message | null = null;

  function pickCandidate(): Message | null {
    const messages = options.messages();
    // Ф8-1 B2: лента у низа — последнее сообщение считается показанным (даже если наблюдатель не отдал его
    // долю: длинное письмо внизу, окно ниже порога).
    if (messages.length > 0 && options.isAtBottom()) return messages[messages.length - 1] ?? null;
    return findBottomVisibleMessage(messages, visibleIds);
  }

  function schedule(): void {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      // Условие «вкладка видима и в фокусе» — в момент отправки (B3: возврат фокуса перезапускает таймер).
      if (document.visibilityState !== 'visible' || !document.hasFocus()) return;
      const candidate = pickCandidate();
      if (candidate !== null && isNewerInDisplayOrder(candidate, lastMarked)) {
        if (options.markRead(candidate)) lastMarked = candidate;
      }
    }, delayMs);
  }

  // Ф8-1 B3: окно было без фокуса, пока шёл таймер, — возврат фокуса (или вкладки) без прокрутки раньше
  // ничего не перезапускал, беседа оставалась непрочитанной.
  const onFocus = (): void => schedule();
  const onVisibility = (): void => {
    if (document.visibilityState === 'visible') schedule();
  };
  window.addEventListener('focus', onFocus);
  document.addEventListener('visibilitychange', onVisibility);

  function attach(newRoot: HTMLElement | null): void {
    observer?.disconnect();
    observer = null;
    visibleIds.clear();
    root = newRoot;
    if (root === null) return;
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const idAttr = (entry.target as HTMLElement).dataset.messageId;
          if (idAttr === undefined) continue;
          const id = Number(idAttr);
          if (isEntryShown(entry, root)) visibleIds.add(id);
          else visibleIds.delete(id);
        }
        schedule();
      },
      { root, threshold: [...READ_OBSERVER_THRESHOLDS] },
    );
    for (const row of root.querySelectorAll<HTMLElement>('[data-message-id]')) {
      observer.observe(row);
    }
  }

  return {
    attach,
    schedule,
    reset(): void {
      lastMarked = null;
    },
    dispose(): void {
      observer?.disconnect();
      observer = null;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
