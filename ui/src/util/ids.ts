// Ф10-7, решение 9 (`docs/architecture.md`): случайные ключи (идемпотентность отправки,
// ключи файлов, `client_id` присутствия). С телефона хаб открыт по http не на localhost — «безопасного
// контекста» нет, и `crypto.randomUUID` там НЕ существует (TypeError ломал отправку). `crypto.getRandomValues`
// работает везде — из него собирается UUID v4 того же вида. Весь `ui/src` берёт ключи только отсюда.

export function newId(): string {
  const c = globalThis.crypto;
  if (typeof c.randomUUID === 'function') return c.randomUUID();
  return uuidV4FromRandom(c.getRandomValues(new Uint8Array(16)));
}

/** UUID v4 из 16 случайных байт (RFC 9562: версия 4, вариант 10xx). Отдельно — ради теста. */
export function uuidV4FromRandom(bytes: Uint8Array): string {
  const b = Uint8Array.from(bytes.subarray(0, 16));
  b[6] = (b[6]! & 0x0f) | 0x40;
  b[8] = (b[8]! & 0x3f) | 0x80;
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
