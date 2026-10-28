// Ф48-2: ОДИН общий опрос данных главной («Ждёт тебя», доска) вместо опроса в каждом смонтированном `HomeView`.
// Подписчики считаются: первый запускает опрос (и сразу грузит данные), последний останавливает.
import { store } from './instance';
import { HOME_POLL_MS } from './store';

let subscribers = 0;
let timer: ReturnType<typeof setInterval> | null = null;
let lastHome: Promise<void> = Promise.resolve();

function loadAll(): Promise<void> {
  const home = store.loadHome();
  void store.loadSessions();
  void store.loadJoins();
  void store.loadRuleRequests();
  void store.loadAgentRequests();
  void store.loadPresses();
  void store.loadHumanRequests();
  void store.loadOwner();
  void store.loadLeaves();
  void store.loadGroupOps();
  return Promise.resolve(home).then(() => undefined);
}

/** Подписаться на опрос. Возвращает загрузку `home` (для прокрутки к якорю) и функцию отписки. */
export function subscribeHomePoll(): { loaded: Promise<void>; release: () => void } {
  subscribers += 1;
  if (subscribers === 1) {
    lastHome = loadAll();
    timer = setInterval(() => {
      lastHome = loadAll();
    }, HOME_POLL_MS);
  }
  let released = false;
  return {
    loaded: lastHome,
    release: () => {
      if (released) return;
      released = true;
      subscribers -= 1;
      if (subscribers === 0 && timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    },
  };
}
