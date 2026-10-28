/// <reference types="vite/client" />
// Витрина: сборка с `VITE_SHOWCASE=1` прячет части интерфейса, которые нужны только в личной раскладке хаба. Ветки
// других источников флаг не трогает (их вырезает экспорт кода). По умолчанию флаг выключен — обычная сборка прежняя.
// Функция, а не константа: тесты переключают флаг через `vi.stubEnv`.

export function isShowcase(): boolean {
  const value = String(import.meta.env.VITE_SHOWCASE ?? '').trim().toLowerCase();
  return value === '1' || value === 'true' || value === 'yes';
}
