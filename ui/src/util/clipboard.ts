// Ф14-9 П4 (`docs/architecture.md`): картинка из буфера обмена кнопкой «Из буфера» — на телефоне
// событие `paste` в поле ввода может не отдать картинку, а `navigator.clipboard.read()` есть только в безопасном
// контексте (https). Кнопка — только на сенсорном экране: на ноутбуке работает обычная вставка Ctrl+V.
import { isTouchScreen } from './submitKeys';

export const CLIPBOARD_NO_IMAGE = 'в буфере нет картинки';

/** Показывать ли «Из буфера»: безопасный контекст, `navigator.clipboard.read` и сенсорный экран. */
export function canPasteFromClipboard(): boolean {
  if (typeof window === 'undefined' || window.isSecureContext !== true) return false;
  if (typeof navigator === 'undefined' || typeof navigator.clipboard?.read !== 'function') return false;
  return isTouchScreen();
}

const EXT_BY_TYPE: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
};

/** `image.png`, для второй и дальше — `image-2.png`; расширение по типу. */
export function clipboardImageName(type: string, index: number): string {
  const ext = EXT_BY_TYPE[type.toLowerCase()] ?? (type.split('/')[1]?.replace(/[^a-z0-9]/gi, '') || 'img');
  return index === 0 ? `image.${ext}` : `image-${index + 1}.${ext}`;
}

/** Картинки из буфера файлами (первый `image/*` тип каждого элемента). Отказ в разрешении — исключение. */
export async function clipboardImages(): Promise<File[]> {
  const items = await navigator.clipboard.read();
  const files: File[] = [];
  for (const item of items) {
    const type = item.types.find((t) => t.toLowerCase().startsWith('image/'));
    if (type === undefined) continue;
    const blob = await item.getType(type);
    files.push(new File([blob], clipboardImageName(type, files.length), { type }));
  }
  return files;
}
