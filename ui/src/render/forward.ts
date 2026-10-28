// Разбор `forward_json` (спека 1d-1, п.3): `{ from_name, from_ext_id, ts }`. Строка «переслано от
// <from_name | from_ext_id>» показывается над текстом. Вход недоверенный — битый JSON/не объект/оба имени
// отсутствуют или пустые — строка просто не показывается (без падения).
export interface ParsedForward {
  label: string;
  /** Ф17-3: автопересылка поста канала в группу обсуждения (помечает коннектор; служебная строка). */
  auto: boolean;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function parseForward(forwardJson: string | null): ParsedForward | null {
  if (!forwardJson) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(forwardJson);
  } catch {
    return null;
  }
  if (!isPlainObject(raw)) return null;

  const fromName = typeof raw.from_name === 'string' && raw.from_name.length > 0 ? raw.from_name : null;
  const fromExtId =
    typeof raw.from_ext_id === 'string' && raw.from_ext_id.length > 0 ? raw.from_ext_id : null;
  const label = fromName ?? fromExtId;
  if (label === null) return null;
  return { label, auto: raw.auto === true };
}
