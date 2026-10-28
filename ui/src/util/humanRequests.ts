// Ф54-1: заявки «нужен человек» — выбор ждущих, порядок, срок, подписи видов. Чистые функции без Vue.
// `site`, `url`, `doing`, `need`, `profile`, `tab`, `role` — чужой ввод: только строками для текстовой интерполяции.
import type { HumanKind, HumanRequest } from '../api/types';

/** Заявка со сроком меньше этого идёт в списке первой. */
export const HUMAN_URGENT_MS = 10 * 60 * 1000;

const KIND_LABELS: Record<HumanKind, string> = {
  captcha: 'Капча',
  login: 'Вход',
  code: 'Код',
  payment: 'Оплата',
  consent: 'Согласие',
  other: 'Другое',
};

export function humanKindLabel(kind: string): string {
  return KIND_LABELS[kind as HumanKind] ?? 'Другое';
}

export function isUrgentHuman(item: HumanRequest, nowMs: number): boolean {
  return item.expires_ts !== null && item.expires_ts - nowMs < HUMAN_URGENT_MS;
}

/** Ждущие заявки: со сроком меньше 10 минут — первыми (по сроку), остальные по номеру. */
export function visibleHumans(items: HumanRequest[], nowMs: number): HumanRequest[] {
  return items
    .filter((h) => h.status === 'pending')
    .sort((a, b) => {
      const ua = isUrgentHuman(a, nowMs) ? 0 : 1;
      const ub = isUrgentHuman(b, nowMs) ? 0 : 1;
      if (ua !== ub) return ua - ub;
      if (ua === 0) return (a.expires_ts ?? 0) - (b.expires_ts ?? 0) || a.id - b.id;
      return a.id - b.id;
    });
}

/** «осталось N мин» / «без срока» / «срок вышел». */
export function humanDeadline(item: HumanRequest, nowMs: number): string {
  if (item.expires_ts === null) return 'без срока';
  const left = item.expires_ts - nowMs;
  if (left <= 0) return 'срок вышел';
  return `осталось ${Math.max(1, Math.ceil(left / 60000))} мин`;
}

/** Кто ответил — мелко, только владельцу. */
export function humanDecidedBy(item: HumanRequest): string | null {
  switch (item.decided_by) {
    case 'ui':
      return 'Владелец';
    case 'agent':
      return 'агент';
    case 'bot':
      return 'владелец (бот)';
    default:
      return null;
  }
}

/** «профиль · вкладка»; пусто — `null`. */
export function humanWhere(item: HumanRequest): string | null {
  const parts = [item.profile, item.tab].filter((p): p is string => typeof p === 'string' && p !== '');
  return parts.length > 0 ? parts.join(' · ') : null;
}
