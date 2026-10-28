// Ф34-2: напоминания по беседам. Чистый редьюсер без Vue (как `joins.ts`): стор передаёт `reactive`-объект.
import type { Reminder } from '../api/types';

export interface ReminderState {
  items: Record<number, Reminder>;
  error: string | null;
}

export function createReminderState(): ReminderState {
  return { items: {}, error: null };
}

/** Строка из `/sync`, REST или главной: старшая по `seq` побеждает (опоздавший REST не откатывает свежее). */
export function applyReminder(state: ReminderState, reminder: Reminder): void {
  const prev = state.items[reminder.id];
  if (prev !== undefined && prev.seq > reminder.seq) return;
  state.items[reminder.id] = reminder;
}
