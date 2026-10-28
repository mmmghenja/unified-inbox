// Ф10-3 (`docs/architecture.md`): реакции владельца из хаба.
//
// Клик — одна операция `{emoji, on}` в ядро (`POST /messages/{id}/reactions`); `reactions_json` правит только
// событие источника. Пока операция в пути, чип показан «ставится» — локальное состояние по `message_id` (здесь,
// не в `/sync`). Операция снимается, когда МОИ реакции в `reactions_json` ей соответствуют (есть/нет этого
// эмодзи, П3); за 60 с без соответствия — строка «не поставилось» под сообщением (до следующего клика или ещё
// минуту). Следующая операция считается от ожидаемого (правда + операции в пути), а не от `reactions_json`.
import { reactive } from 'vue';

import { ApiError, NetworkError } from '../api/client';
import type { Message } from '../api/types';
import { type ParsedReaction, type ReactionSource, parseReactions, reactionKey } from '../render/reactions';
import { store } from './instance';

export const REACTION_PENDING_MS = 60_000;
export const REACTION_ERROR_SHOW_MS = 60_000;
export const REACTION_NOT_SET_TEXT = 'не поставилось';
/** Ф15-1 D: реакция на письмо ждёт в ядре 10 с (окно «передумал»); в это окно повторный клик — отмена. */
export const MAIL_REACTION_DELAY_MS = 10_000;
export const MAIL_CANNOT_UNDO_TEXT = 'в письме реакцию после отправки не снять';

export interface PendingReaction {
  id: number;
  emoji: string;
  on: boolean;
  at: number;
}

let nextOpId = 1;

interface ReactionState {
  /** Беседа сообщения — чтобы по сроку найти правду в сторе, даже если строка размонтирована (ревью). */
  channelId: number | null;
  ops: PendingReaction[];
  error: string | null;
  errorAt: number;
}

/** Состояние по `message_id`; реактивно — строка сообщения перерисуется сама. */
export const reactionStates = reactive<Record<number, ReactionState>>({});

function stateOf(messageId: number): ReactionState {
  let state = reactionStates[messageId];
  if (state === undefined) {
    reactionStates[messageId] = { channelId: null, ops: [], error: null, errorAt: 0 };
    state = reactionStates[messageId]!;
  }
  return state;
}

export function pendingOps(messageId: number): PendingReaction[] {
  return reactionStates[messageId]?.ops ?? [];
}

export function reactionError(messageId: number): string | null {
  return reactionStates[messageId]?.error ?? null;
}

/** Мои реакции по правде (`me`), ключами без U+FE0F; свои эмодзи Telegram (`custom`) не в счёт. */
function actualMine(parsed: ParsedReaction[]): Set<string> {
  return new Set(parsed.filter((r) => r.me && !r.custom).map((r) => reactionKey(r.raw)));
}

/** Источники, где у человека одна реакция на сообщение: новая заменяет прежнюю (Telegram без премиума, WhatsApp). */
export function singleReaction(source: ReactionSource | null): boolean {
  return source === 'telegram' || source === 'whatsapp';
}

/** Ожидаемые мои реакции: правда + операции в пути по порядку. Telegram и WhatsApp держат одну свою — `on X`
 * заменяет набор целиком (П1). */
export function expectedMine(source: ReactionSource, parsed: ParsedReaction[], ops: PendingReaction[]): Set<string> {
  let mine = actualMine(parsed);
  for (const op of ops) {
    const key = reactionKey(op.emoji);
    if (op.on) {
      if (singleReaction(source)) mine = new Set([key]);
      else mine.add(key);
    } else {
      mine.delete(key);
    }
  }
  return mine;
}

export interface DisplayReaction extends ParsedReaction {
  pending: boolean;
}

/** Чипы с учётом операций в пути: своя отметка и счётчик — ожидаемые, отличие от правды — «ставится»; новая
 * реакция, которой ещё нет на сообщении, — отдельный чип «ставится». */
export function displayReactions(
  source: ReactionSource | null,
  parsed: ParsedReaction[],
  ops: PendingReaction[],
): DisplayReaction[] {
  if (source === null || ops.length === 0) return parsed.map((r) => ({ ...r, pending: false }));
  const expected = expectedMine(source, parsed, ops);
  const out: DisplayReaction[] = parsed.map((r) => {
    if (r.custom) return { ...r, pending: false };
    const mine = expected.has(reactionKey(r.raw));
    const count = r.count + (mine && !r.me ? 1 : 0) - (!mine && r.me ? 1 : 0);
    return { ...r, me: mine, count, pending: mine !== r.me };
  });
  const present = new Set(parsed.filter((r) => !r.custom).map((r) => reactionKey(r.raw)));
  for (const op of ops) {
    const key = reactionKey(op.emoji);
    if (!op.on || present.has(key) || !expected.has(key)) continue;
    present.add(key);
    out.push({ label: op.emoji, raw: op.emoji, count: 1, me: true, custom: false, pending: true });
  }
  return out;
}

/** П3: снять операции, которым правда уже соответствует. Вызывается при каждом изменении `reactions_json`. */
export function settleReactions(messageId: number, reactionsJson: string | null): void {
  const state = reactionStates[messageId];
  if (state === undefined || state.ops.length === 0) return;
  const mine = actualMine(parseReactions(reactionsJson));
  state.ops = state.ops.filter((op) => op.on !== mine.has(reactionKey(op.emoji)));
}

function setError(messageId: number, text: string): void {
  const state = stateOf(messageId);
  const at = Date.now();
  state.error = text;
  state.errorAt = at;
  setTimeout(() => {
    const current = reactionStates[messageId];
    if (current !== undefined && current.errorAt === at) current.error = null;
  }, REACTION_ERROR_SHOW_MS);
}

/** Сообщение в сторе: лента беседы или загруженные треды. */
function findStoredMessage(channelId: number | null, messageId: number): Message | null {
  if (channelId === null) return null;
  const sync = store.state.sync;
  const inFeed = sync.messagesByChannel[channelId]?.find((m) => m.id === messageId);
  if (inFeed !== undefined) return inFeed;
  for (const thread of Object.values(sync.threads)) {
    if (thread.parent?.id === messageId) return thread.parent;
    const reply = thread.replies.find((m) => m.id === messageId);
    if (reply !== undefined) return reply;
  }
  return null;
}

/** Срок П3: операции старше 60 с без соответствия правде снимаются, под сообщением — «не поставилось». Перед
 * этим — сверка с правдой из стора (ревью: строка могла быть размонтирована, когда правда пришла). */
export function expireReactions(messageId: number): void {
  const state = reactionStates[messageId];
  if (state === undefined) return;
  const stored = findStoredMessage(state.channelId, messageId);
  if (stored !== null) settleReactions(messageId, stored.reactions_json);
  const now = Date.now();
  const alive = state.ops.filter((op) => now - op.at < REACTION_PENDING_MS);
  if (alive.length === state.ops.length) return;
  state.ops = alive;
  setError(messageId, REACTION_NOT_SET_TEXT);
}

function failureText(err: unknown, status?: string): string {
  if (status === 'off') return 'реакции для этого аккаунта выключены';
  if (err instanceof ApiError) {
    if (err.status === 409 && err.message === 'unsupported') return 'в этом чате нельзя ставить реакции';
    if (err.status === 409 && err.message === 'sent, cannot undo') return MAIL_CANNOT_UNDO_TEXT;
    if (err.status === 409 && err.message === 'sending') return 'реакция уже отправляется — не снять';
    if (err.status === 409 && err.message === 'not google') return 'адресат не на Gmail — реакцией не ответить';
    if (err.status === 409 && err.message === 'too many') return 'на письме уже 20 моих реакций';
    if (err.status === 409 && err.message === 'receive only') return 'ящик подключён только на приём';
    if (err.status === 409) return 'на это сообщение реакцию не поставить';
    if (err.status === 422 && err.message === 'not in telegram set') return 'в Telegram такой реакции нет';
    if (err.status === 422) return 'такую реакцию поставить нельзя';
    if (err.status === 404) return 'сообщение недоступно';
  }
  if (err instanceof NetworkError) return 'нет связи с хабом — реакция не отправлена';
  return 'реакция не отправлена';
}

/** Клик по быстрому ряду или по чипу: переключить эмодзи от ОЖИДАЕМОГО состояния (П3). Telegram — новая `on`
 * заменяет прежние `on` в пути (у Telegram одна своя реакция). */
export async function toggleReaction(
  messageId: number,
  source: ReactionSource,
  reactionsJson: string | null,
  emoji: string,
  channelId: number | null = null,
): Promise<void> {
  const state = stateOf(messageId);
  if (channelId !== null) state.channelId = channelId;
  state.error = null;
  const parsed = parseReactions(reactionsJson);
  const key = reactionKey(emoji);
  const on = !expectedMine(source, parsed, state.ops).has(key);
  if (source === 'mail' && !on) {
    // Ф15-1 D: в письме снять можно только ещё не ушедшую реакцию — свою `on` в окне 10 с (отмена).
    const pendingOn = state.ops.find((o) => o.on && reactionKey(o.emoji) === key);
    if (pendingOn === undefined || Date.now() - pendingOn.at >= MAIL_REACTION_DELAY_MS) {
      setError(messageId, MAIL_CANNOT_UNDO_TEXT);
      return;
    }
  }
  const op: PendingReaction = { id: nextOpId++, emoji, on, at: Date.now() };
  state.ops = [
    ...state.ops.filter((o) => reactionKey(o.emoji) !== key && !(singleReaction(source) && on && o.on)),
    op,
  ];
  settleReactions(messageId, reactionsJson);
  setTimeout(() => expireReactions(messageId), REACTION_PENDING_MS);

  let status: 'queued' | 'off';
  try {
    status = await store.setReaction(messageId, emoji, on);
  } catch (err) {
    console.error('reaction failed', err);
    state.ops = state.ops.filter((o) => o.id !== op.id);
    setError(messageId, failureText(err));
    return;
  }
  if (status === 'off') {
    state.ops = state.ops.filter((o) => o.id !== op.id);
    setError(messageId, failureText(null, status));
  }
}
