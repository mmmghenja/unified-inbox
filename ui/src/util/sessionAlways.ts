// Ф99-7.2: «Всегда в этой сессии» — подпись ключа набора сессии (`mcp:<инструмент>` | `dir:<каталог>`).
export const ALWAYS_BUTTON = 'Всегда в этой сессии';

export function alwaysLabel(key: string): string {
  if (key.startsWith('mcp:')) return `инструмент ${key.slice(4)}`;
  if (key.startsWith('dir:')) return `запись в каталог ${key.slice(4)}`;
  return key;
}

/** Подсказка кнопки на карточке: что именно перестанет спрашиваться. */
export function alwaysHint(key: string): string {
  return `Разрешить и больше не спрашивать в этой сессии: ${alwaysLabel(key)}. Снять — «⋯ → Разрешения…»`;
}
