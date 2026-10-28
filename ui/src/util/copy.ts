// Ф10-7, решение 9: копирование в буфер обмена. `navigator.clipboard` есть только в «безопасном контексте»
// (https или localhost); с телефона хаб открыт по http внутри VPN — там его нет. Тогда — старый путь: скрытое
// поле `textarea` с текстом, выделение и `document.execCommand('copy')` (работает по жесту пользователя).
// Поле оформлено классом `.copy-buffer` (`styles/base.css`) — без inline-стилей (CSP `style-src 'self'`).

/** `true` — скопировано; `false` — браузер не дал (текст остаётся на экране, его можно выделить руками). */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && typeof navigator.clipboard?.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }
  return copyViaTextarea(text);
}

function copyViaTextarea(text: string): boolean {
  const area = document.createElement('textarea');
  area.value = text;
  area.readOnly = true; // телефон не открывает клавиатуру
  area.className = 'copy-buffer';
  area.setAttribute('aria-hidden', 'true');
  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.body.appendChild(area);
  try {
    area.select();
    area.setSelectionRange(0, text.length); // iOS Safari не выделяет по одному `select()`
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    area.remove();
    active?.focus();
  }
}
