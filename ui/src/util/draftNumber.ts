// Номер черновика / сообщения в очереди отправки — `id` строки outbox (его называют `hub_draft`, `hub_draft_new`,
// `hub_drafts`; сессии пишут владельцу «отмени № 998»). Поиск «№» в списках черновиков и очереди: оставляет строки,
// чей номер равен набранному (нет такого — оканчивается на него): «998», «#998», «№ 998»; без цифр — список целиком.

/** Цифры из поля «№»; пусто — фильтра нет. */
export function numberQuery(raw: string): string {
  return raw.replace(/\D/g, '');
}

export function filterByNumber<T extends { id: number }>(items: readonly T[], raw: string): T[] {
  const digits = numberQuery(raw);
  if (digits === '') return [...items];
  // Точное совпадение номера важнее: «6» — это №6, а не хвост у 13-значных номеров боевых бесед; нет точного —
  // номера, оканчивающиеся на набранное («8774» -> 1099511628774).
  const exact = items.filter((item) => String(item.id) === digits);
  return exact.length > 0 ? exact : items.filter((item) => String(item.id).endsWith(digits));
}
