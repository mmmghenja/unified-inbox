// Ф55-1 (интерфейс): серая строка-причина у блока «в очереди» окна сессии — почему реплики ещё не ушли.
// Считается на клиенте из того, что уже есть в сторе; порядок причин — по спеке, прерванная реплика первой.
import type { HubSession, SessionRequest, SessionSay } from '../api/types';
import { engineProfile } from './engineProfiles';
import { SESSION_WAIT_LABEL } from './sessions';

export const SAY_TOOL_ONLY_REASON = 'возьмёт при следующем вызове инструмента, иначе — следующим ходом';

export function sayQueueReason(
  session: Pick<HubSession, 'state' | 'engine' | 'wait_reason' | 'pending_requests' | 'closed_ts'>,
  requests: Pick<SessionRequest, 'status'>[],
  says: Pick<SessionSay, 'status'>[],
): string | null {
  if (says.length === 0 || session.closed_ts !== null) return null;
  if (says.some((s) => s.status === 'interrupted')) return 'ждёт твоего решения по прерванной реплике';
  if (requests.some((r) => r.status === 'pending') || (requests.length === 0 && session.pending_requests > 0)) {
    return 'уйдёт вместе с твоим ответом на запрос';
  }
  if (session.state === 'running') {
    // Ф74-1: по `mid_turn` профиля — Claude Code и Cursor (Ф71-1 6б) берут реплики посреди хода, OpenCode — нет.
    // Ф74-8: Cursor берёт её только на ближайшем вызове инструмента (`mid_turn_tool_only`); не было — следующим ходом.
    const profile = engineProfile(session.engine);
    if (profile?.mid_turn_tool_only) return SAY_TOOL_ONLY_REASON;
    return profile?.mid_turn ? 'сессия возьмёт после текущего шага' : 'уйдёт, когда закончится ход';
  }
  if (session.wait_reason) return SESSION_WAIT_LABEL[session.wait_reason];
  if (session.state === 'asleep') return 'сессия поднимается';
  return null;
}
