// Давность последнего кадра потока (спека: «давность последнего события потока — тикает раз в секунду,
// после 60 с без heartbeat полоса краснеет»). Heartbeat ядро шлёт раз в 15 с (`feed.py`, `heartbeat_s=15.0`),
// именованным кадром `event: hb` (решение архитектора 20.09 — не SSE-комментарием, который EventSource не
// отдаёт в JS). Порог застывания — 60 с, с запасом в 4 heartbeat-интервала.
import { reactive } from 'vue';

export const STALE_THRESHOLD_MS = 60_000;

export interface StalenessState {
  lastFrameAt: number | null;
  stale: boolean;
}

export function createStaleness(): StalenessState {
  return reactive({ lastFrameAt: null, stale: false });
}

/** Любой кадр потока (`hb`, `message`, `channel`) и `onopen` — признак жизни: сдвигает давность и гасит
 * «лента не обновляется» немедленно, не дожидаясь следующего тика часов. */
export function recordFrame(state: StalenessState, now: number = Date.now()): void {
  state.lastFrameAt = now;
  state.stale = false;
}

export function tick(state: StalenessState, now: number = Date.now()): void {
  state.stale = state.lastFrameAt === null || now - state.lastFrameAt > STALE_THRESHOLD_MS;
}

/** Тикающие часы полосы давности — раз в секунду, независимо от кадров потока (иначе после ПОСЛЕДНЕГО
 * кадра индикатор молчал бы вечно, никогда не показывая, что лента застыла). */
export function startStalenessClock(state: StalenessState, intervalMs = 1000): () => void {
  const id = setInterval(() => tick(state), intervalMs);
  return () => clearInterval(id);
}
