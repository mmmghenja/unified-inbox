// Ф8-3 D3 (`docs/architecture.md` и поправка П8): «Владелец смотрит эту беседу». Пока окно видимо,
// в фокусе и открыта беседа — `POST /presence` сразу и раз в 20 с (ядро держит запись 45 с); смена беседы или
// треда — сразу новое; потеря фокуса, скрытие вкладки, закрытие беседы — `channel_id: null` (снимает запись
// ТОЛЬКО этой вкладки: у каждой вкладки свой случайный `client_id`). Панель треда — `thread_ext_id`.
//
// Без Vue: трекер получает «что открыто» и «активно ли окно» функциями, стор зовёт `update()` при смене
// беседы/треда, фокус и видимость трекер слушает сам. Ошибки сети глотаются (присутствие — подсказка
// разборщику, а не данные): следующий удар сердца через 20 с пришлёт его заново.

import { newId } from '../util/ids';

export const PRESENCE_HEARTBEAT_MS = 20_000;

export interface PresenceTarget {
  channelId: number;
  threadExtId: string | null;
}

export interface PresenceDeps {
  post: (clientId: string, channelId: number | null, threadExtId: string | null) => Promise<unknown>;
  /** Открытая беседа (и тред, если открыта его панель) — независимо от фокуса. */
  target: () => PresenceTarget | null;
  /** Окно видимо и в фокусе. */
  isActive: () => boolean;
  clientId?: string;
}

function newClientId(): string {
  return newId(); // Ф10-7: без безопасного контекста — v4 из `getRandomValues`
}

function keyOf(target: PresenceTarget | null): string | null {
  return target === null ? null : `${target.channelId}:${target.threadExtId ?? ''}`;
}

export function defaultIsActive(): boolean {
  return document.visibilityState === 'visible' && document.hasFocus();
}

export class PresenceTracker {
  readonly clientId: string;
  private readonly deps: PresenceDeps;
  /** Ключ последнего отправленного: `undefined` — ещё ничего не отправляли, `null` — отправили «снять». */
  private sentKey: string | null | undefined = undefined;
  private timer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  constructor(deps: PresenceDeps) {
    this.deps = deps;
    this.clientId = deps.clientId ?? newClientId();
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    document.addEventListener('visibilitychange', this.listener);
    window.addEventListener('focus', this.listener);
    window.addEventListener('blur', this.listener);
    this.update();
  }

  /** Остановка (выход, истёкшая сессия): без запроса — запись в ядре истечёт сама через 45 с. */
  stop(): void {
    if (!this.started) return;
    this.started = false;
    document.removeEventListener('visibilitychange', this.listener);
    window.removeEventListener('focus', this.listener);
    window.removeEventListener('blur', this.listener);
    this.clearTimer();
    this.sentKey = undefined;
  }

  private listener = (): void => {
    this.update();
  };

  private current(): PresenceTarget | null {
    return this.deps.isActive() ? this.deps.target() : null;
  }

  /** Пересчитать и, если изменилось, отправить сразу; удар сердца — только пока есть что держать. */
  update(): void {
    if (!this.started) return;
    const target = this.current();
    const key = keyOf(target);
    if (key !== this.sentKey && !(key === null && this.sentKey === undefined)) {
      this.send(target);
      this.sentKey = key;
      this.clearTimer();
    }
    if (target !== null && this.timer === null) {
      this.timer = setInterval(() => this.heartbeat(), PRESENCE_HEARTBEAT_MS);
    } else if (target === null) {
      this.clearTimer();
    }
  }

  private heartbeat(): void {
    const target = this.current();
    if (target === null || keyOf(target) !== this.sentKey) {
      this.update();
      return;
    }
    this.send(target);
  }

  private send(target: PresenceTarget | null): void {
    this.deps
      .post(this.clientId, target?.channelId ?? null, target?.threadExtId ?? null)
      .catch((err: unknown) => console.error('presence: не отправлено', err));
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
