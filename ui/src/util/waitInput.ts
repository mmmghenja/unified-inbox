// Ф41-5 (интерфейс): один вход для `buildWaiting` из стора. «Ждёт тебя · N» на компьютере (панель, список, счётчик
// проекта) и бейдж вкладки «Ждёт» телефона считаются по одному и тому же набору — новый вид карточек не может
// попасть в одно место и пропасть из другого.
import type { Store } from '../store/store';
import type { WaitingInput } from './waiting';

export function waitingInputOf(store: Pick<Store, 'state'>): WaitingInput {
  const s = store.state;
  return {
    projects: s.projects,
    home: s.home,
    sessions: s.sessions,
    joins: Object.values(s.joins.items),
    ruleRequests: Object.values(s.ruleRequests.items),
    agentRequests: Object.values(s.agentRequests.items),
    presses: Object.values(s.presses.items),
    humans: Object.values(s.humans.items),
    leaves: Object.values(s.leaves.items),
    groupOps: Object.values(s.groupOps.items),
    reminders: Object.values(s.reminders.items),
  };
}
