// Ф21-1b: состояние людей в интерфейсе. Открытая карточка (по контакту — клик по имени отправителя, или по
// человеку — из панели «Люди») и контакты, у которых ядро ответило 404 `not_person`/`is_me`: имя такого контакта
// больше не кликается до перезагрузки страницы (память только в этой странице, не в хранилище браузера).
import { reactive } from 'vue';

export type PersonTarget = { contactId: number } | { personId: number };

export const peopleUi = reactive<{ card: PersonTarget | null; notPerson: Record<number, true> }>({
  card: null,
  notPerson: {},
});

export function openPersonOfContact(contactId: number): void {
  peopleUi.card = { contactId };
}

export function openPerson(personId: number): void {
  peopleUi.card = { personId };
}

export function closePerson(): void {
  peopleUi.card = null;
}

export function markNotPerson(contactId: number): void {
  peopleUi.notPerson[contactId] = true;
}

/** Имя отправителя кликается: есть `sender.id`, сообщение не моё, ядро не отвечало «не человек». */
export function senderClickable(
  message: { is_me: boolean; sender: { id: number } | null },
): boolean {
  const sender = message.sender;
  return sender !== null && typeof sender.id === 'number' && !message.is_me && peopleUi.notPerson[sender.id] !== true;
}
