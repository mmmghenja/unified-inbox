// Ф9-2 (9b), Ф78-1: чистые функции окна сессии хаба — подписи событий, разбор входа запроса и команда
// терминала (дорожек задач по «Го» больше нет). Без Vue.
//
// Всё, что приходит от сессии (заголовки, суть, события, вход инструмента, итог), — недоверенный ввод: здесь
// только строки; показ — текстовой интерполяцией Vue.
import type { SessionRequest, WorkEvent, WorkSayPriority } from '../api/types';

export const SAY_PRIORITY_LABELS: Record<WorkSayPriority, string> = {
  next: 'отправить',
  later: 'после хода',
  now: 'срочно',
};

// --- события ---------------------------------------------------------------------------------------------

export type EventTone = 'tool' | 'error' | 'text' | 'note' | 'turn' | 'warn' | 'done';

export interface EventView {
  tone: EventTone;
  text: string;
}

/** B, лента: как показать событие. Только строки — разметки нет. */
export function eventView(event: Pick<WorkEvent, 'tool' | 'summary' | 'is_error'> & { kind: string }): EventView {
  const summary = event.summary ?? '';
  switch (event.kind) {
    case 'tool': {
      const tool = event.tool ?? '?';
      return { tone: 'tool', text: summary && summary !== tool ? `▸ ${tool}: ${summary}` : `▸ ${tool}` };
    }
    case 'tool_done':
      return event.is_error ? { tone: 'error', text: `✗ ${summary}` } : { tone: 'done', text: summary };
    case 'text':
      return { tone: 'text', text: summary };
    case 'request':
      return { tone: 'note', text: `попросила: ${summary}` };
    case 'decision':
      return { tone: 'note', text: summary.replace(/^Владелец /, 'ты ') };
    case 'say': {
      // 9c: реплика оркестратора — «оркестратор: …» вместо «ты написал: …».
      const m = /^(владелец|Оркестратор) \((next|later|now)\): ([\s\S]*)$/.exec(summary);
      if (m) {
        const label = m[2] === 'next' ? '' : ` (${SAY_PRIORITY_LABELS[m[2] as WorkSayPriority]})`;
        const who = m[1] === 'Оркестратор' ? 'оркестратор' : 'ты написал';
        return { tone: 'note', text: `${who}${label}: ${m[3]}` };
      }
      return { tone: 'note', text: summary };
    }
    case 'turn_end': {
      const m = /(\d+) с/.exec(summary);
      const text = m ? `ход окончен · ${m[1]} с` : summary || 'ход окончен';
      return { tone: event.is_error ? 'warn' : 'turn', text: event.is_error ? `${text} · ${summary}` : text };
    }
    case 'error':
    case 'warn':
      return { tone: 'warn', text: summary };
    default:
      return { tone: 'note', text: summary };
  }
}

/** B: реплика на протухший запрос (кнопка «Написать: разрешено, повтори»). */
export const STALE_RETRY_TEXT = 'Разрешено, повтори.';

// --- запросы ---------------------------------------------------------------------------------------------

export interface QuestionOption {
  label: string;
  description: string | null;
}

export interface QuestionItem {
  question: string;
  header: string | null;
  options: QuestionOption[];
  multiSelect: boolean;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

/** `AskUserQuestion`: `input.questions[]` — `{question, header?, options: [{label, description?}], multiSelect?}`.
 * Неверная форма — пропускается (вопрос без текста не показывается). */
export function parseQuestions(input: unknown): QuestionItem[] {
  const questions = asRecord(input)?.questions;
  if (!Array.isArray(questions)) return [];
  const result: QuestionItem[] = [];
  for (const raw of questions) {
    const q = asRecord(raw);
    if (q === null || typeof q.question !== 'string' || q.question === '') continue;
    const options: QuestionOption[] = [];
    for (const rawOpt of Array.isArray(q.options) ? q.options : []) {
      const o = asRecord(rawOpt);
      if (o !== null && typeof o.label === 'string' && o.label !== '') {
        options.push({ label: o.label, description: typeof o.description === 'string' ? o.description : null });
      }
    }
    result.push({
      question: q.question,
      header: typeof q.header === 'string' ? q.header : null,
      options,
      multiSelect: q.multiSelect === true,
    });
  }
  return result;
}

export interface PermissionInputView {
  /** Bash — команда целиком (моноширинно); иначе `null`. */
  command: string | null;
  /** Описание команды Bash (`description`), если есть. */
  description: string | null;
  /** Вход целиком JSON-ом (для MCP и прочих; у Bash — остальные поля, если они есть). */
  json: string | null;
}

/** B, разрешение: вход ЦЕЛИКОМ. При обрезке ядро кладёт `{_truncated: "<начало JSON>"}` — показывается как есть. */
export function permissionInputView(request: Pick<SessionRequest, 'tool' | 'input'>): PermissionInputView {
  const input = asRecord(request.input);
  if (input !== null && typeof input._truncated === 'string') {
    return { command: null, description: null, json: input._truncated };
  }
  if (request.tool === 'Bash' && input !== null && typeof input.command === 'string') {
    const rest = Object.fromEntries(
      Object.entries(input).filter(([k]) => k !== 'command' && k !== 'description'),
    );
    return {
      command: input.command,
      description: typeof input.description === 'string' ? input.description : null,
      json: Object.keys(rest).length > 0 ? JSON.stringify(rest, null, 2) : null,
    };
  }
  return { command: null, description: null, json: JSON.stringify(request.input ?? {}, null, 2) };
}

// --- терминал --------------------------------------------------------------------------------------------

/** Аргумент для POSIX-оболочки: безопасные символы — как есть, иначе в одинарных кавычках. */
export function shellQuote(value: string): string {
  if (/^[\w@%+=:,./-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** B, «Открыть копию в терминале»: `null`, пока нет рабочей папки или id сессии. */
export function terminalCommand(workdir: string | null | undefined, sessionId: string | null | undefined): string | null {
  if (!workdir || !sessionId) return null;
  return `cd ${shellQuote(workdir)} && claude --resume ${shellQuote(sessionId)} --fork-session`;
}
