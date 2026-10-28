// Окно черновика, открытое листанием (владелец 12.10.2026): черновики проекта смотрятся подряд, а строка, с которой
// окно открыли, исчезает вместе со своим черновиком после отправки — поэтому листание и переход «к следующему
// после отправки» держит общий хозяин `DraftModalHost` (id черновика здесь), а не строка.
import { reactive } from 'vue';

import type { OutboxListItem } from '../api/types';

/** `id` — черновик в окне общего хозяина; `viewing` — черновик, открытый сейчас в любом окне (строки или
 * хозяина): когда он уходит из списка (отправлен, отклонён), хозяин открывает следующий черновик проекта. */
export const draftModal = reactive<{ id: number | null; viewing: number | null }>({ id: null, viewing: null });

/** Черновики того же проекта, что `item`, в порядке столбца главной (старые первыми); `item` нет в списке
 * (подтверждённый по графику, устаревший список) — только он сам. */
export function draftSiblings(all: OutboxListItem[], item: OutboxListItem): OutboxListItem[] {
  const list = all
    .filter((d) => d.project_id === item.project_id)
    .sort((a, b) => a.created_ts - b.created_ts || a.id - b.id);
  return list.some((d) => d.id === item.id) ? list : [item];
}

/** Куда перейти после того, как `id` ушёл из списка: следующий по прежнему порядку, иначе предыдущий. */
export function nextAfter(order: number[], id: number, left: Set<number>): number | null {
  const at = order.indexOf(id);
  if (at < 0) return null;
  for (const candidate of [...order.slice(at + 1), ...order.slice(0, at).reverse()]) {
    if (left.has(candidate)) return candidate;
  }
  return null;
}
