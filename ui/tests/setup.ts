// Общий setup для vitest: jsdom не реализует IntersectionObserver (использует его `MessageFeed.vue` для
// отслеживания «реально показанных» сообщений) — стаб нужен только чтобы монтирование компонентов в тестах
// не падало; поведение самого наблюдателя тестами не проверяется (это UI-механика, не пункт спеки 7–12).
class IntersectionObserverStub implements IntersectionObserver {
  readonly root: Element | Document | null = null;
  readonly rootMargin: string = '';
  readonly thresholds: ReadonlyArray<number> = [];

  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

if (typeof globalThis.IntersectionObserver === 'undefined') {
  globalThis.IntersectionObserver = IntersectionObserverStub as unknown as typeof IntersectionObserver;
}
