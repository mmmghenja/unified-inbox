// Ф58-1 B: состояние окна «Написать…» — открыто ли, какой адресат выбран и к какой карточке очереди относится
// реплика (`ref`). Хозяин окна — `WriteModalHost.vue` (монтируется в `MainApp.vue`), как у окна черновика.
import { reactive } from 'vue';

export type WriteTarget =
  /** `branch` — ветка разговора с агентом (Ф66-2): id сессии хаба, `null` — «Общее»; без него — как выбрана. */
  | { kind: 'owner_channel'; branch?: number | null }
  | { kind: 'session'; id: number }
  | { kind: 'agent'; key: string };

export interface WriteCardRef {
  ref: string;
  title: string;
}

export const writeModal = reactive<{ open: boolean; target: WriteTarget | null; card: WriteCardRef | null }>({
  open: false,
  target: null,
  card: null,
});

/** Открыть окно; без адресата — список (на компьютере сам выбирает «Мой агент»). */
export function openWrite(target: WriteTarget | null = null, card: WriteCardRef | null = null): void {
  writeModal.target = target;
  writeModal.card = card;
  writeModal.open = true;
}

export function closeWrite(): void {
  writeModal.open = false;
  writeModal.target = null;
  writeModal.card = null;
}

export function sameTarget(a: WriteTarget | null, b: WriteTarget | null): boolean {
  if (a === null || b === null || a.kind !== b.kind) return false;
  if (a.kind === 'session' && b.kind === 'session') return a.id === b.id;
  if (a.kind === 'agent' && b.kind === 'agent') return a.key === b.key;
  return true;
}
