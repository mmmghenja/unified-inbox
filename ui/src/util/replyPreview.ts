// Общая метка «<имя>: <начало текста>» — используется и плашкой «Ответ …» в композере (Telegram, спека 2e,
// раздел «Семантика «Ответить»»), и цитатой «↪ <имя>: <начало текста>» над сообщением-ответом (тот же раздел,
// «Лента канала»). Источник данных — недоверенный текст собеседника: вызывающая сторона обязана вставлять
// результат ТОЛЬКО обычной текстовой интерполяцией Vue (`{{ }}`), без вставки сырого HTML, как и весь
// остальной текст в UI (правило проекта, тест-сторож `tests/11-guard.spec.ts`).
import type { Message } from '../api/types';

const PREVIEW_LENGTH = 80;

export function replyPreviewLabel(message: Message): string {
  const name = message.is_me ? 'я' : (message.sender?.name ?? message.sender?.handle ?? '(без имени)');
  const text = (message.text ?? '').slice(0, PREVIEW_LENGTH);
  return `${name}: ${text}`;
}
