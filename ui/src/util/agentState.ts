// Ф50-4 (интерфейс): состояние сессии хаба для оркестратора и владельца — у кого её карточки и на чём она стоит.
// Источник — `agent_state` из `GET /sessions` (в `/sync` его нет). Тексты ролей и причин — чужой ввод: только `{{ }}`.
import type { HubSession, Project } from '../api/types';
import { SESSION_DAY_NOTE, isDayClosed } from './workday';

const ORIGIN_WORDS: Record<string, string> = {
  say: 'по твоей реплике',
  signal: 'по сигналу',
  self: 'продолжает сама',
  compact: 'сжатие',
};

/** Пометка о карточках: «ждёт тебя: 2»; `null` — карточек нет. */
export function cardsNote(session: HubSession): string | null {
  const cards = session.agent_state?.cards;
  if (cards === undefined || cards === null) return null;
  return cards.user > 0 ? `ждёт тебя: ${cards.user}` : null;
}

/** Ф64-1: «спит: день закрыт» — по полю состояния сессии или по проекту (`projects` — из стора); иначе `null`. */
export function dayNote(session: HubSession, projects: Project[]): string | null {
  const st = session.agent_state;
  if (typeof st?.day_closed_ts === 'number') return SESSION_DAY_NOTE;
  if (session.state === 'asleep' && isDayClosed(projects.find((p) => p.id === session.project_id))) return SESSION_DAY_NOTE;
  return null;
}

/** Куски строки состояния: ход, шаг, сигнал в очереди, ждущие сигналы, причина сна. */
export function agentStateParts(session: HubSession, nowMs: number): string[] {
  const st = session.agent_state;
  if (st === undefined || st === null) return [];
  const parts: string[] = [];
  if (typeof st.day_closed_ts === 'number' && st.state === 'asleep') parts.push(SESSION_DAY_NOTE);
  if (st.state === 'running' && st.turn) {
    const min = Math.floor(Math.max(0, nowMs - st.turn.since_ts) / 60_000);
    const origin = ORIGIN_WORDS[st.turn.origin];
    parts.push(`${min < 1 ? 'в работе меньше минуты' : `в работе ${min} мин`}${origin ? ` (${origin})` : ''}`);
    if (st.step?.tool) parts.push(`шаг: ${st.step.tool}`);
  }
  if (st.mid_queued_ts !== null && st.mid_queued_ts !== undefined) parts.push('сигнал подан, ждёт конца инструмента');
  if (st.signals && st.signals.waiting > 0) {
    parts.push(`сигналов ждёт: ${st.signals.waiting}${st.signals.urgent > 0 ? ` (срочных ${st.signals.urgent})` : ''}`);
  }
  if (st.state === 'asleep' && st.sleep?.text) parts.push(st.sleep.text);
  return parts;
}
