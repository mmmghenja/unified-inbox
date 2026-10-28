// Ф38-2 (раздел C): заявки сессий на правило раскладки — что показывать карточкой и как назвать строки. Чистые функции.
// `description`, `reason`, `who`, `examples`, `account_title` — чужой текст: показывается только строками.
import type { RuleRequest } from '../api/types';
import { besed } from './mapping';

/** Решённая заявка (заведено / отклонено) видна сутки после решения; ядро отдаёт решённые за 7 дней. */
export const RULE_REQUEST_RESULT_TTL_MS = 24 * 60 * 60 * 1000;

export function ruleRequestVisible(request: RuleRequest, nowMs: number): boolean {
  if (request.status === 'pending') return true;
  if (request.status === 'approved' || request.status === 'declined') {
    return request.decided_ts !== null && nowMs - request.decided_ts <= RULE_REQUEST_RESULT_TTL_MS;
  }
  return false;
}

export function visibleRuleRequests(requests: RuleRequest[], nowMs: number): RuleRequest[] {
  return requests.filter((r) => ruleRequestVisible(r, nowMs)).sort((a, b) => a.id - b.id);
}

export function ruleSourceName(source: string | null): string | null {
  if (source === null || source === '') return null;
  if (source === 'telegram') return 'Telegram';
  if (source === 'slack') return 'Slack';
  if (source === 'mattermost') return 'Mattermost';
  if (source === 'whatsapp') return 'WhatsApp';
  if (source === 'mail') return 'Почта';
  return source;
}

/** «Почта · e@x.ru: все письма от x.ru → Umbrella»; название аккаунта нет — «аккаунт N». */
export function ruleHeadline(request: RuleRequest, projectName: string): string {
  const source = ruleSourceName(request.account_source);
  const title =
    request.account_title !== null && request.account_title !== '' ? request.account_title : `аккаунт ${request.account_id}`;
  const account = source === null ? title : `${source} · ${title}`;
  return `${account}: ${request.description} → ${projectName}`;
}

/** «переложит 3 беседы: a, b, c»; 0 — «сейчас не переложит…»; `moved` нет — строки нет. */
export function ruleMovedLine(request: RuleRequest): string | null {
  const moved = request.moved;
  if (moved === null || moved === undefined) return null;
  if (moved === 0) return 'сейчас не переложит ни одной беседы, будет действовать на новые';
  const examples = request.examples ?? [];
  const head = `переложит ${besed(moved, true)}`;
  return examples.length > 0 ? `${head}: ${examples.join(', ')}` : head;
}

/** Итог решённой заявки: «заведено, переложено N» / «отклонено». */
export function ruleOutcomeLine(request: RuleRequest): string | null {
  if (request.status === 'declined') return 'отклонено';
  if (request.status === 'approved') {
    return request.moved === null || request.moved === undefined ? 'заведено' : `заведено, переложено ${request.moved}`;
  }
  return null;
}
