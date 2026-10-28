// Разбор `mail_to_json` (Ф11-8, docs/architecture.md): JSON-список адресатов (To+Cc) СВОЕГО
// письма. Строка «кому: a, b, c и ещё N» показывается над текстом тем же приглушённым стилем, что «переслано
// от»; полный список — в подсказке (`title`). Вход недоверенный: битый JSON, не список, нет ни одной непустой
// строки — строки «кому» нет (без падения). Выводится только текстом (интерполяция Vue), без HTML.
export const MAIL_TO_SHOWN = 3;

export interface ParsedMailTo {
  label: string;
  title: string;
}

export function parseMailTo(mailToJson: string | null | undefined): ParsedMailTo | null {
  if (!mailToJson) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(mailToJson);
  } catch {
    return null;
  }
  if (!Array.isArray(raw)) return null;
  const addrs = raw.filter((a): a is string => typeof a === 'string' && a.length > 0);
  if (addrs.length === 0) return null;
  const shown = addrs.slice(0, MAIL_TO_SHOWN).join(', ');
  const rest = addrs.length - MAIL_TO_SHOWN;
  return {
    label: rest > 0 ? `${shown} и ещё ${rest}` : shown,
    title: addrs.join(', '),
  };
}
