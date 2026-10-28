// Ф90-1: состояние сессии одной точкой в шапке окна (вместо чипа и строки подписки): цвет — тон, подпись — по
// наведению и в «Сведениях» меню. Чистая функция без Vue.
import type { HubSession } from '../api/types';
import { hasLiveBg, sessionStateText } from './sessionBg';
import { INTERRUPTED_STATE, SESSION_STATE_LABEL, SESSION_WAIT_LABEL, hasInterrupted } from './sessions';

export type HeadTone = 'wait' | 'run' | 'idle' | 'silent';

export interface HeadState {
  text: string;
  tone: HeadTone;
  /** Сессия ждёт владельца (карточка, тяжёлая, пауза, прерванная реплика). */
  waits: boolean;
}

export function sessionHeadState(session: HubSession, pendingRequests: number): HeadState {
  const waits =
    pendingRequests > 0 ||
    session.wait_reason === 'heavy' ||
    session.wait_reason === 'paused' ||
    hasInterrupted(session);
  let text: string;
  if (waits && hasInterrupted(session) && !session.wait_reason) text = INTERRUPTED_STATE;
  else if (waits) text = session.wait_reason ? SESSION_WAIT_LABEL[session.wait_reason] : 'ждёт тебя';
  else if (session.wait_reason) text = SESSION_WAIT_LABEL[session.wait_reason];
  else text = session.state === 'running' ? 'работает' : sessionStateText(session, SESSION_STATE_LABEL);
  let tone: HeadTone = waits ? 'wait' : session.state === 'running' || hasLiveBg(session) ? 'run' : 'idle';
  // Живая, но не слушающая подписку сессия — отдельный тон: сигналы ей не придут.
  if (tone === 'idle' && session.state === 'idle' && !session.wait_reason && (session.sub === null || session.listening === false)) {
    tone = 'silent';
    text = 'не слушает подписку';
  }
  return { text, tone, waits };
}
