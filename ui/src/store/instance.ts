// Единственный экземпляр стора на приложение — импортируется компонентами напрямую (роутера и pinia нет,
// спека прямо это запрещает; один reactive-стор проще provide/inject ради одного потребителя).
import { setSessionEnginesSource } from '../util/engineProfiles';
import { createStore } from './store';

export const store = createStore();

// Ф74-1: профили движков сессий хаба — из `/status` этого стора (реактивно: читается при каждом обращении).
setSessionEnginesSource(() => store.state.status?.session_engines);
