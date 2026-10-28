// Ф20-3 A1 (`docs/architecture.md`): «Владелец работает в хабе» — окно видимо и в фокусе И было
// действие пользователя (клавиша, мышь, касание, прокрутка) за последние 5 минут. Тогда раз в 60 с —
// `POST /presence/active` (ядро по нему держит статус Slack «На связи»). Открытая, но брошенная вкладка запросов
// не шлёт. Отдельно от присутствия в беседе (`presence.ts`): та подсказка разборщику шлёт каждые 20 с и без
// действий. Ошибки сети глотаются — следующий круг через 60 с.

export const ACTIVITY_HEARTBEAT_MS = 60_000;
export const ACTIVITY_IDLE_MS = 5 * 60_000;

const ACTION_EVENTS = ['keydown', 'pointerdown', 'mousedown', 'mousemove', 'touchstart', 'wheel', 'scroll'] as const;

export interface ActivityDeps {
  post: () => Promise<unknown>;
  /** Окно видимо и в фокусе. */
  isActive: () => boolean;
  now?: () => number;
}

export class ActivityTracker {
  private readonly deps: ActivityDeps;
  private lastAction: number | null = null;
  private lastPost: number | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  constructor(deps: ActivityDeps) {
    this.deps = deps;
  }

  private now(): number {
    return this.deps.now ? this.deps.now() : Date.now();
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    for (const name of ACTION_EVENTS) window.addEventListener(name, this.onAction, { capture: true, passive: true });
    this.timer = setInterval(() => this.tick(), ACTIVITY_HEARTBEAT_MS);
  }

  stop(): void {
    if (!this.started) return;
    this.started = false;
    for (const name of ACTION_EVENTS) window.removeEventListener(name, this.onAction, { capture: true });
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.lastAction = null;
    this.lastPost = null;
  }

  private onAction = (): void => {
    const now = this.now();
    this.lastAction = now;
    // Первое действие после паузы — сразу, не дожидаясь круга (не чаще раза в 60 с).
    if (this.lastPost === null || now - this.lastPost >= ACTIVITY_HEARTBEAT_MS) this.tick();
  };

  private tick(): void {
    if (this.lastAction === null || !this.deps.isActive()) return;
    const now = this.now();
    if (now - this.lastAction >= ACTIVITY_IDLE_MS) return;
    this.lastPost = now;
    this.deps.post().catch((err: unknown) => console.error('activity: не отправлено', err));
  }
}
