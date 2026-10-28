// Ф8-3 D1/D2 (`docs/architecture.md`): подписи режимов беседы и выбор счётчика для списков.
import type { AttentionMode, EffectiveAttention } from '../api/types';

export const ATTENTION_LABELS: Record<EffectiveAttention, string> = {
  react: 'Реагирую',
  info: 'К сведению (упоминания будят)',
  muted: 'Заглушено',
};

/** Порядок пунктов выбора в шапке беседы (после «Авто»). */
export const ATTENTION_CHOICES: readonly AttentionMode[] = ['react', 'info'];

export const AUTO = 'auto';

export function autoLabel(auto: EffectiveAttention | undefined): string {
  return auto === undefined ? 'Авто' : `Авто (сейчас: ${ATTENTION_LABELS[auto].toLowerCase()})`;
}

export type BadgeKind = 'react' | 'quiet' | 'plain';

export interface UnreadBadge {
  kind: BadgeKind;
  count: number;
}

/** D2: `unread_react` > 0 — красный счётчик с ним; иначе прочее непрочитанное — серый. Поле не пришло (ядро
 * старее Ф8-3) — как раньше, обычный бейдж. `null` — бейджа нет. */
export function unreadBadge(unreadReact: number | undefined, unread: number): UnreadBadge | null {
  if (unreadReact === undefined) return unread > 0 ? { kind: 'plain', count: unread } : null;
  if (unreadReact > 0) return { kind: 'react', count: unreadReact };
  return unread > 0 ? { kind: 'quiet', count: unread } : null;
}
