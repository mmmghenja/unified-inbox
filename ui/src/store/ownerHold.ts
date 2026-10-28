// Владелец 15.10.2026: карточка очереди «Агент», которую он смотрит (окно, «развернуть») или по которой начал отвечать,
// не исчезает, когда ядро убрало её из очереди (сессия сделала новый ход — `superseded`, решил агент, сняли). Она
// остаётся на месте с плашкой «Есть новее»: набранный текст цел, можно скопировать, перейти к новой или закрыть.
// Карточка сама сообщает, что занята (`holdCard`), и снимает это при ответе, закрытии и размонтировании.
import { reactive } from 'vue';

import type { OwnerItem } from '../api/types';

interface Held {
  item: OwnerItem;
  owner: number;
}

export const ownerHold = reactive<{ cards: Record<string, Held> }>({ cards: {} });

let owners = 0;
/** Свой номер каждой смонтированной карточке: одна и та же карточка может стоять в двух окнах (столбец и телефон). */
export function nextHoldOwner(): number {
  owners += 1;
  return owners;
}

/** Карточка занята (`item` — её последнее состояние) или свободна (`null`). Свободной снимает только своё. */
export function holdCard(ref: string, owner: number, item: OwnerItem | null): void {
  if (item !== null) {
    ownerHold.cards[ref] = { item, owner };
    return;
  }
  if (ownerHold.cards[ref]?.owner === owner) delete ownerHold.cards[ref];
}

export interface ShownCard {
  item: OwnerItem;
  /** Ядро карточку из очереди уже убрало; держится, пока владелец с ней работает. */
  gone: boolean;
}

/** Живая очередь плюс занятые карточки, которых в ней больше нет (`gone`). */
export function withHeld(live: OwnerItem[], held: Record<string, Held>): ShownCard[] {
  const refs = new Set(live.map((i) => i.ref));
  const gone = Object.entries(held)
    .filter(([ref]) => !refs.has(ref))
    .map(([, h]) => ({ item: h.item, gone: true }));
  return [...live.map((item) => ({ item, gone: false })), ...gone];
}

/** Что пришло на смену ушедшей карточке: карточка той же сессии не старше её, сначала того же вида. */
export function successorOf(gone: OwnerItem, live: OwnerItem[]): OwnerItem | null {
  if (gone.session_id === null) return null;
  const same = live.filter(
    (i) => i.ref !== gone.ref && i.session_id === gone.session_id && i.created_ts >= gone.created_ts,
  );
  return same.find((i) => i.kind === gone.kind) ?? same[0] ?? null;
}

/** Ответ по ушедшей карточке ещё имеет смысл: ход сессии, вытесненный следующим, ядро принимает репликой сессии. */
export function answerableGone(item: OwnerItem): boolean {
  return item.kind === 'turn';
}

/** Текст плашки ушедшей карточки. */
export function goneNotice(item: OwnerItem, successor: OwnerItem | null): string {
  if (item.kind === 'turn') {
    return successor !== null
      ? 'Есть новее: сессия сделала следующий ход. Ответ отсюда всё равно уйдёт ей репликой.'
      : 'Карточки уже нет в очереди. Ответ отсюда уйдёт сессии репликой, если на ход ещё не ответили.';
  }
  return successor !== null
    ? 'Есть новее: у этой сессии новая карточка. Эта устарела — ответить по ней нельзя.'
    : 'Карточки уже нет в очереди: решено или снято. Ответить по ней нельзя.';
}
