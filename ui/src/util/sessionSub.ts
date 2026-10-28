// Ф41-4 (`docs/architecture.md`): подписка постоянной сессии хаба — поля формы и их
// сборка в `want_sub`, строки состояния подписки в окне, подписи «поднять»/«сжать». Чистые функции без Vue.
// Ключевые слова — чужой ввод владельца, но показываются только текстом.
import type { HubSession, SessionSub, SessionWantSub } from '../api/types';

export type SubKind = 'none' | 'cards' | 'messages' | 'direct';
export type SubAccountsMode = 'all' | 'only' | 'except';

export const SUB_KIND_LABEL: Record<SubKind, string> = {
  none: 'не подписывать',
  cards: 'карточки разборщика',
  messages: 'сообщения',
  direct: 'только сообщения агентов',
};

export const SUB_MODE_LABEL: Record<'cards' | 'messages' | 'direct', string> = {
  cards: 'карточки разборщика',
  messages: 'сообщения',
  direct: 'сообщения агентов',
};

export const SUB_APPLY_NOTE = 'применится при следующем подъёме сессии';
export const BATCH_MAX_MIN = 120; // Ф67-1: как у ядра (`keywords.BATCH_MIN_MAX`)

/** Поля блока «Подписка» (форма новой сессии и правка в окне). Строки — как в полях ввода. */
export interface SubForm {
  kind: SubKind;
  personal: boolean;
  keywords: string;
  exclude: string;
  batch: string;
  accountsMode: SubAccountsMode;
  accounts: string;
}

export function emptySubForm(kind: SubKind = 'none'): SubForm {
  return { kind, personal: false, keywords: '', exclude: '', batch: '', accountsMode: 'all', accounts: '' };
}

/** Умолчание блока: у сессии с ролью — «только сообщения агентов», без роли — «не подписывать». */
export function defaultSubKind(role: string): SubKind {
  return role.trim() === '' ? 'none' : 'direct';
}

function words(text: string): string[] {
  return text
    .split(',')
    .map((w) => w.trim())
    .filter((w) => w !== '');
}

export type BuiltSub = { ok: true; value: SessionWantSub | null } | { ok: false; error: string };

/** Форма -> `want_sub`. Лёгкие проверки на клиенте: номера аккаунтов — целые, пачка — целое 0–1440; остальное
 * проверяет ядро (его `422` показывается как есть). */
export function buildWantSub(form: SubForm): BuiltSub {
  if (form.kind === 'none') return { ok: true, value: null };
  if (form.kind !== 'messages') return { ok: true, value: { mode: form.kind } };
  const batchText = form.batch.trim();
  let batch: number | null = null;
  if (batchText !== '') {
    if (!/^\d+$/.test(batchText) || Number(batchText) > BATCH_MAX_MIN) {
      return { ok: false, error: `Пачка — целое число минут от 0 до ${BATCH_MAX_MIN}.` };
    }
    batch = Number(batchText);
  }
  let accounts: SessionWantSub['accounts'] = null;
  if (form.accountsMode !== 'all') {
    const parts = words(form.accounts);
    if (parts.length === 0 || parts.some((p) => !/^\d+$/.test(p))) {
      return { ok: false, error: 'Аккаунты — номера целыми числами через запятую.' };
    }
    const nums = parts.map(Number);
    accounts = form.accountsMode === 'only' ? { only: nums } : { except: nums };
  }
  return {
    ok: true,
    value: {
      mode: 'messages',
      msg_filter: form.personal ? 'personal' : 'all',
      keywords: words(form.keywords),
      exclude: words(form.exclude),
      batch_min: batch,
      accounts,
    },
  };
}

/** Желаемая подписка из записи сессии -> поля формы (для правки в окне). */
export function subFormOf(want: SessionWantSub | null | undefined): SubForm {
  if (!want) return emptySubForm('none');
  const form = emptySubForm(want.mode);
  form.personal = want.msg_filter === 'personal';
  form.keywords = (want.keywords ?? []).join(', ');
  form.exclude = (want.exclude ?? []).join(', ');
  form.batch = want.batch_min === null || want.batch_min === undefined ? '' : String(want.batch_min);
  const acc = want.accounts;
  if (acc && 'only' in acc) {
    form.accountsMode = 'only';
    form.accounts = acc.only.join(', ');
  } else if (acc && 'except' in acc) {
    form.accountsMode = 'except';
    form.accounts = acc.except.join(', ');
  }
  return form;
}

// --- строки состояния --------------------------------------------------------------------------------------

export const SUB_NONE = 'не подписана';
export const SUB_AT_WAKE = 'подпишется при первом подъёме';
export const SUB_STALE = 'настройки изменены — применятся после сна, можно усыпить';
export const SUB_HOST_SILENT = 'хост не отвечает';
export const SUB_LISTENING = 'слушает';

/** Ф67-1: «пачкой раз в 10 мин» — действующий интервал пачки сигналов подписки; 0, нет или не число — пусто. */
export function batchLabel(minutes: number | null | undefined): string {
  return typeof minutes === 'number' && Number.isInteger(minutes) && minutes > 0 ? `пачкой раз в ${minutes} мин` : '';
}

/** «подписка #7 · сообщения · ключи: цена, договор · пачкой раз в 15 мин · ждёт сигналов: 2». */
export function subSummary(sub: SessionSub): string {
  const parts = [`подписка #${sub.id}`, SUB_MODE_LABEL[sub.mode] ?? sub.mode];
  if (sub.keywords.length > 0) parts.push(`ключи: ${sub.keywords.join(', ')}`);
  const batch = batchLabel(sub.batch_min);
  if (batch) parts.push(batch);
  parts.push(`ждёт сигналов: ${sub.pending}`);
  return parts.join(' · ');
}

export interface SubLine {
  text: string;
  /** Состояние — «слушает» / «хост не отвечает» (только у живой подписки). */
  state: string | null;
  /** Строка про расхождение `want_sub` и подписки. */
  stale: string | null;
  /** Строка про беседы «без подтверждения» у роли. */
  autosend: string | null;
}

export function autosendNote(count: number): string {
  return `у роли ${count} бесед без подтверждения: сессия будет отвечать в них сама`;
}

export function subLine(session: Pick<HubSession, 'want_sub' | 'sub' | 'sub_stale' | 'listening' | 'autosend_channels'>): SubLine {
  const autosend = (session.autosend_channels ?? 0) > 0 ? autosendNote(session.autosend_channels!) : null;
  const sub = session.sub ?? null;
  if (sub === null) {
    return { text: session.want_sub ? SUB_AT_WAKE : SUB_NONE, state: null, stale: null, autosend };
  }
  let state: string | null = null;
  if (session.listening === true) state = SUB_LISTENING;
  else if (session.listening === false) state = SUB_HOST_SILENT;
  return { text: subSummary(sub), state, stale: session.sub_stale === true ? SUB_STALE : null, autosend };
}

// --- «тяжёлая» сессия ---------------------------------------------------------------------------------------

/** Сессия ждёт подъёма: сигналы есть, но контекст большой и кэш остыл (причина ожидания `heavy`). */
export function heavyCount(session: Pick<HubSession, 'wait_reason' | 'sub' | 'closed_ts'>): number {
  if (session.closed_ts !== null || session.wait_reason !== 'heavy') return 0;
  return session.sub?.pending ?? 0;
}

export function heavyLine(count: number): string {
  return `${count} сигналов ждут: контекст большой, кэш остыл`;
}

export function heavyWaitLabel(name: string, count: number): string {
  return `сессия ${name}: ${count} сигналов ждут подъёма`;
}

export const CONTEXT_UNKNOWN = 'контекст: уточнится после хода';

/** П19: пауза лимита подписки держит реплики и сигналы — владелец может отправить их сразу. */
export function pausedWaiting(session: Pick<HubSession, 'wait_reason' | 'sub' | 'queued_says'>, saysQueued: number): boolean {
  return session.wait_reason === 'paused' && (saysQueued > 0 || session.queued_says > 0 || (session.sub?.pending ?? 0) > 0);
}

export const PAUSED_LINE = 'пауза лимита подписки: реплика ждёт';
