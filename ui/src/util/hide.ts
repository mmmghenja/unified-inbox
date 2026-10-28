// Ф8-4 B4 (`docs/architecture.md`): «Удалить у себя» — с подтверждением, у источника
// (Telegram/Slack/Mattermost) ничего не удаляется. Общий вход для строки сообщения, альбома и шапки беседы.
import { ApiError } from '../api/client';
import { store } from '../store/instance';

export const HIDE_MESSAGE_CONFIRM = 'Удалить сообщение из хаба? В Telegram/Slack/Mattermost оно останется.';
export const HIDE_CHANNEL_CONFIRM = 'Удалить беседу из хаба? В Telegram/Slack/Mattermost она останется.';

function failureText(err: unknown): string {
  if (err instanceof ApiError && err.status === 409) {
    return 'Не удалено: сообщение ещё отправляется — дождитесь итога отправки.';
  }
  return 'Не удалось удалить — попробуйте ещё раз.';
}

function pluralReplies(n: number): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return 'ответов';
  if (mod10 === 1) return 'ответ';
  if (mod10 >= 2 && mod10 <= 4) return 'ответа';
  return 'ответов';
}

/** Ревью Ф8-4 (П6): скрытие родителя треда уносит все его текущие ответы — подтверждение говорит об этом. */
export function hideMessageConfirmText(replyCount: number): string {
  if (replyCount <= 0) return HIDE_MESSAGE_CONFIRM;
  return `Удалить сообщение и ${replyCount} ${pluralReplies(replyCount)} в треде из хаба? В Telegram/Slack/Mattermost они останутся.`;
}

/** Одно сообщение или все части альбома (П11). `replyCount` — ответов в треде родителя (`thread.reply_count`),
 * они скрываются вместе с ним. `false` — отменено или не получилось. */
export async function confirmAndHideMessages(messageIds: number[], replyCount = 0): Promise<boolean> {
  if (!window.confirm(hideMessageConfirmText(replyCount))) return false;
  try {
    await store.hideMessages(messageIds);
    return true;
  } catch (err) {
    console.error('hide message failed', err);
    window.alert(failureText(err));
    return false;
  }
}

export async function confirmAndHideChannel(channelId: number): Promise<boolean> {
  if (!window.confirm(HIDE_CHANNEL_CONFIRM)) return false;
  try {
    await store.hideChannel(channelId);
    return true;
  } catch (err) {
    console.error('hide channel failed', err);
    window.alert(failureText(err));
    return false;
  }
}
