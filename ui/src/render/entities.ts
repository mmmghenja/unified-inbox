// Разбор `entities_json`. ВАЖНО (см. отчёт исполнителя 6b): ядро (`src/hub/core/events.py`,
// `IngestEvent.entities: list | dict | None`) НЕ определяет формат entity вообще — это непрозрачный JSON,
// который коннектор кладёт как есть (`ingest.py: "entities_json": _dump(ev.entities)`). Ни фиксированной
// схемы полей, ни единиц offset/length в кодовой базе НЕТ — они появятся только с коннектором Telegram (Ф1).
//
// Решение исполнителя 6b (пока нет коннектора, задающего факт): схема entity — как у Telegram
// (`MessageEntity`): `{ type, offset, length, url? }`, offset/length — в единицах UTF-16 code units.
// Это совпадает со внутренним представлением строк в JS (строка уже последовательность UTF-16 code units,
// `str.length`/`str.slice()` считают в них же) — при рендере в браузере НИКАКОЙ конвертации не нужно, а
// куски за пределами BMP (например, эмодзи вне BMP, суррогатная пара) естественно попадают в длину 2, как и
// у Telegram. Если Ф1 принесёт другую единицу (кодовые точки Юникода или байты UTF-8, как у части API) —
// этот модуль единственное место, которое придётся поменять. Это вопрос к архитектору (см. отчёт).
//
// Устойчивость: вход — недоверенный (пришёл от собеседника). Любая entity, которая не проходит валидацию
// (неизвестный `type`, нечисловые/отрицательные/нулевые `offset`/`length`, выход за пределы текста,
// пересечение с уже принятой entity) — просто ИГНОРИРУЕТСЯ, весь текст всё равно показывается целиком.
// Само `entities_json` может быть отсутствующим/`null`/битым JSON/не-массивом — тогда сообщение рендерится
// как обычный текст без оформления.

export type EntityType = 'bold' | 'italic' | 'code' | 'pre' | 'link' | 'mention';

const KNOWN_TYPES: ReadonlySet<string> = new Set<EntityType>([
  'bold',
  'italic',
  'code',
  'pre',
  'link',
  'mention',
]);

export interface TextEntity {
  type: EntityType;
  offset: number;
  length: number;
  url?: string;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isValidRange(offset: unknown, length: unknown, textLength: number): offset is number {
  return (
    typeof offset === 'number' &&
    Number.isInteger(offset) &&
    typeof length === 'number' &&
    Number.isInteger(length) &&
    offset >= 0 &&
    length > 0 &&
    offset + length <= textLength
  );
}

/** Разобрать и провалидировать `entities_json` для текста длиной `textLength` (UTF-16 code units).
 * Возвращает entity, отсортированные по `offset`, БЕЗ пересечений (первая по offset выигрывает). */
export function parseEntities(entitiesJson: string | null, textLength: number): TextEntity[] {
  if (!entitiesJson) return [];

  let raw: unknown;
  try {
    raw = JSON.parse(entitiesJson);
  } catch {
    return [];
  }
  if (!Array.isArray(raw)) return [];

  const candidates: TextEntity[] = [];
  for (const item of raw) {
    if (!isPlainObject(item)) continue;
    const type = item.type;
    if (typeof type !== 'string' || !KNOWN_TYPES.has(type)) continue;
    if (!isValidRange(item.offset, item.length, textLength)) continue;
    const entity: TextEntity = { type: type as EntityType, offset: item.offset, length: item.length as number };
    if (type === 'link') {
      if (typeof item.url !== 'string') continue; // ссылка без url — бессмысленна, entity отбрасывается
      entity.url = item.url;
    }
    candidates.push(entity);
  }

  candidates.sort((a, b) => a.offset - b.offset || a.length - b.length);

  const accepted: TextEntity[] = [];
  let lastEnd = 0;
  for (const entity of candidates) {
    if (entity.offset < lastEnd) continue; // пересечение с уже принятой — игнорируется целиком
    accepted.push(entity);
    lastEnd = entity.offset + entity.length;
  }
  return accepted;
}

const SAFE_LINK_PROTOCOLS: ReadonlySet<string> = new Set(['http:', 'https:', 'mailto:']);

/** `new URL(href)` + белый список схем (спека, «Уточнения архитектора», раздел «Ссылка»). Схема (в т.ч.
 * `JaVaScRiPt:`, ведущие пробелы/управляющие символы, `\t` внутри схемы) нормализуется самим WHATWG URL-
 * парсером (обязательный шаг спецификации: обрезка C0-controls/space по краям, удаление TAB/CR/LF из всей
 * строки) ДО того, как мы читаем `url.protocol` — отдельная предобработка не нужна и не должна дублироваться.
 * Относительные адреса (`new URL` без base) кидают исключение — тоже отбрасываются. */
export function safeHref(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  return SAFE_LINK_PROTOCOLS.has(url.protocol) ? url.href : null;
}
