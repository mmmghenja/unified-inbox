// Ф66-5: навигация по тредам беседы. Клик по беседе, где нового в основной ленте нет, а внутри есть свежий тред
// (`fresh_thread_ext`), открывает сразу панель этого треда. Есть непрочитанное в основной ленте — как раньше.
import type { Channel } from '../api/types';

export function threadTargetOf(channel: Channel | undefined | null): string | null {
  if (channel === undefined || channel === null) return null;
  if ((channel.unread_top ?? channel.unread) > 0) return null;
  const ext = channel.fresh_thread_ext;
  return typeof ext === 'string' && ext !== '' ? ext : null;
}

/** Метка в строке беседы: «💬 свежий тред» / «💬 N тредов». Пусто, если есть бейдж непрочитанного в тредах
 * (`💬N`) — метку не дублируем. */
export function freshThreadLabel(channel: Channel): string | null {
  const n = channel.fresh_threads ?? 0;
  if (n <= 0 || (channel.unread_threads ?? 0) > 0) return null;
  return n === 1 ? '💬 свежий тред' : `💬 ${n} тредов`;
}
