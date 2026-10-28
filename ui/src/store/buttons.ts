// Ф44-1b/1d: нажатие инлайн-кнопки бота Telegram. Показ кнопок — `components/MessageButtons.vue`; состояние
// нажатий — `store/presses.ts`, запрос и решения — методы стора. Адрес кнопки — `(id сообщения, ряд, место)`,
// ряд и место с 1; `text` — текст кнопки из выдачи (ядро сверяет его с хранимым).
import { store } from './instance';

export function pressButton(messageId: number, row: number, col: number, text: string): void {
  void store.pressButton(messageId, row, col, text);
}
