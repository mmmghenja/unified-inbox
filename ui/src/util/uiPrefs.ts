// Ф10-6, решение 7 (`docs/architecture.md`): запоминание выбора интерфейса (свёрнут ли оркестратор) в
// `localStorage`. Единственное место в `ui/src`, где разрешено хранилище браузера (исключение сторожа
// `tests/11-guard.spec.ts`): только флаги вида по ключам `hub.*`, никаких токенов, текстов и данных переписки
// (Ф12-5: ещё id выбранного аккаунта — число, не адрес).
// Чтение и запись — в `try/catch`: хранилище недоступно (приватный режим, запрет) — работаем без запоминания.

/** Флаг по ключу: `true`/`false`, `null` — выбора нет или хранилище недоступно. */
export function readUiFlag(key: `hub.${string}`): boolean | null {
  try {
    const value = window.localStorage.getItem(key);
    return value === '1' ? true : value === '0' ? false : null;
  } catch {
    return null;
  }
}

export function writeUiFlag(key: `hub.${string}`, value: boolean): void {
  try {
    window.localStorage.setItem(key, value ? '1' : '0');
  } catch {
    // хранилище недоступно — работаем без запоминания
  }
}

/** Ф12-5 C2: выбранный id (аккаунт «Откуда» формы «Новое сообщение» по проекту) — целое > 0, иначе `null`. */
export function readUiId(key: `hub.${string}`): number | null {
  try {
    const value = Number(window.localStorage.getItem(key));
    return Number.isInteger(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

export function writeUiId(key: `hub.${string}`, value: number): void {
  try {
    window.localStorage.setItem(key, String(value));
  } catch {
    // хранилище недоступно — работаем без запоминания
  }
}

/** Ф34-1: короткая строка выбора (фильтр стадий проекта) — `null`, если не задано или хранилище недоступно. */
export function readUiString(key: `hub.${string}`): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeUiString(key: `hub.${string}`, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // хранилище недоступно — работаем без запоминания
  }
}
