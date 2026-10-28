// Ф39 C: подпись «кто слушает беседу» — у беседы теперь может быть несколько живых подписок.
import type { ChanSub } from '../api/types';

/** Одна подписка: роль (при `role_no > 1` — с номером), без роли — имя сессии, без имени — `#<pid>`. */
export function listenerName(sub: ChanSub): string {
  if (sub.role) {
    return sub.role_no != null && sub.role_no > 1 ? `${sub.role} ${sub.role_no}` : sub.role;
  }
  // Ф41-2а: у сессии хаба pid условный (отрицательный) — показывается её номер.
  if (sub.held && sub.hub_session_id != null) return sub.session_name || `сессия хаба #${sub.hub_session_id}`;
  return sub.session_name || `#${sub.session_pid}`;
}

/** «слушает: маркетолог» / «слушают: маркетолог, сисадмин»; никого — `null`. */
export function listeningLabelOf(subs: ChanSub[], channelId: number): string | null {
  const rows = subs.filter((s) => s.channel_id === channelId && s.ended_ts === null && s.live !== false);
  if (rows.length === 0) return null;
  const names = rows.map(listenerName).join(', ');
  return rows.length === 1 ? `слушает: ${names}` : `слушают: ${names}`;
}
