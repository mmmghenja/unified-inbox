// Ф41-3а: строки шагов окна сессии по `brief` события и разбор unified diff. Чистые функции без Vue.
// Всё, что приходит от хоста (пути, вывод, текст diff), — чужой ввод: только строки, без разметки.
import type { SessionBrief, SessionEvent, SessionUsage } from '../api/types';
import { eventView, type EventView } from './dispatch';
import { CONTEXT_UNKNOWN } from './sessionSub';

export const COST_TITLE_CLAUDE = 'условная цена по тарифам API; по подписке деньги не списываются';

function plural(n: number, one: string, few: string, many: string): string {
  const m100 = n % 100;
  const m10 = n % 10;
  if (m100 >= 11 && m100 <= 14) return many;
  if (m10 === 1) return one;
  if (m10 >= 2 && m10 <= 4) return few;
  return many;
}

export function linesLabel(n: number): string {
  return `${n} ${plural(n, 'строка', 'строки', 'строк')}`;
}

export function stepsLabel(n: number): string {
  return `${n} ${plural(n, 'шаг', 'шага', 'шагов')}`;
}

/** «12 с», «1 мин 5 с»; меньше секунды — «< 1 с». */
export function durationLabel(ms: number): string {
  const sec = Math.round(ms / 1000);
  if (sec < 1) return '< 1 с';
  if (sec < 60) return `${sec} с`;
  const min = Math.floor(sec / 60);
  const rest = sec % 60;
  return rest === 0 ? `${min} мин` : `${min} мин ${rest} с`;
}

/** «$0.03»; меньше цента — «< $0.01». */
export function costLabel(usd: number): string {
  return usd > 0 && usd < 0.01 ? '< $0.01' : `$${usd.toFixed(2)}`;
}

function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function briefOf(event: SessionEvent): SessionBrief | null {
  const b = event.brief;
  return b !== null && b !== undefined && typeof b === 'object' ? b : null;
}

export interface StepView extends EventView {
  /** Раскрывается по клику (есть что показать запросом одного события). */
  expandable: boolean;
}

/** Строка шага сессии. Событие без `brief` — как прежде (`eventView`); подробности — только если `has_detail`. */
export function stepView(event: SessionEvent): StepView {
  let base = eventView(event);
  const expandable = event.has_detail === true;
  const brief = briefOf(event);
  if (brief === null) return { ...base, expandable };
  // Ф92-1 (A1): реплика агента владельца (`brief.by = 'agent'`) — «Агент за тебя», а не «ты написал».
  if (event.kind === 'say' && (brief as { by?: unknown }).by === 'agent') {
    base = { ...base, text: base.text.replace(/^ты написал(?: \([^)]*\))?: /, 'Агент за тебя: ') };
    if (brief.type !== 'files') return { ...base, expandable };
  }
  if (event.kind === 'tool_done' && !event.is_error) {
    if (brief.type === 'diff' && typeof brief.file === 'string' && isNum(brief.added) && isNum(brief.removed)) {
      return { tone: 'done', text: `✓ ${brief.file} +${brief.added} −${brief.removed}`, expandable };
    }
    if (brief.type === 'output' && isNum(brief.lines)) {
      return { tone: 'done', text: `готово · ${linesLabel(brief.lines)}`, expandable };
    }
  }
  if (event.kind === 'say' && brief.type === 'files' && Array.isArray(brief.files)) {
    const names = (brief.files as { name?: unknown }[]).map((f) => String(f?.name ?? '')).filter((n) => n !== '');
    if (names.length > 0) return { ...base, text: `${base.text.replace(/\s+$/, '')}\n📎 ${names.join(', ')}`, expandable };
  }
  if (event.kind === 'status' && brief.type === 'signal') {
    const b = brief as Record<string, unknown>;
    const parts = SIGNAL_PARTS.flatMap(([key, label]) => (isNum(b[key]) && (b[key] as number) > 0 ? [`${label} ${b[key]}`] : []));
    return { tone: 'note', text: parts.length > 0 ? `сигнал хаба: ${parts.join(', ')}` : 'сигнал хаба', expandable };
  }
  if (event.kind === 'status' && brief.type === 'mode') {
    const mode = (brief as { mode?: unknown }).mode;
    return { tone: 'note', text: mode === 'auto' ? 'режим: авто' : 'режим: ручной', expandable };
  }
  if (event.kind === 'status' && brief.type === 'auto_answer') {
    const q = (brief as { question?: unknown }).question;
    const text = typeof q === 'string' && q !== '' ? `вопрос сессии — авто: решает сама · ${q}` : 'вопрос сессии — авто: решает сама';
    return { tone: 'note', text, expandable };
  }
  if (event.kind === 'status' && brief.type === 'autopilot') {
    const t = (brief as { text?: unknown }).text;
    return { tone: 'note', text: typeof t === 'string' && t !== '' ? t : 'настройки автопилота изменились', expandable };
  }
  if (event.kind === 'compact' && brief.type === 'compact') {
    const before = (brief as { before?: unknown }).before;
    const text = isNum(before) ? `контекст сжат: было ${thousands(before)} тыс.` : 'контекст сжат';
    return { tone: 'note', text, expandable };
  }
  if (event.kind === 'turn_end' && brief.type === 'usage' && isNum(brief.duration_ms)) {
    const parts = ['ход окончен', durationLabel(brief.duration_ms)];
    if (isNum(brief.steps)) parts.push(stepsLabel(brief.steps));
    if (isNum(brief.cost_usd)) parts.push(costLabel(brief.cost_usd));
    return { tone: event.is_error ? 'warn' : 'turn', text: parts.join(' · '), expandable: false };
  }
  return { ...base, expandable };
}

// --- строка сессии: контекст, кэш, цена -------------------------------------------------------------------

const SIGNAL_PARTS: [string, string][] = [
  ['cards', 'карточек'],
  ['messages', 'сообщений'],
  ['reminders', 'напоминаний'],
  ['agents', 'сообщений агентов'],
  ['from_user', 'от владельца'],
  ['presses', 'исходов нажатий'],
  ['drafts', 'исходов черновиков'],
  ['channels', 'бесед'],
];

function thousands(tokens: number): string {
  return tokens < 1000 ? String(tokens) : String(Math.round(tokens / 1000));
}

export type ContextTone = 'normal' | 'warn' | 'danger';

export interface UsageLabel {
  context: string;
  contextTone: ContextTone;
  cost: string | null;
}

function costOf(usage: SessionUsage): string | null {
  return usage.cost_usd !== null && usage.cost_usd !== undefined ? `всего ${costLabel(usage.cost_usd)}` : null;
}

/** Ф48-2 D: настройки сжатия сессии для строки контекста (поля записи сессии). */
export interface CompactInfo {
  compact_at?: number | null;
  compact_tokens?: number | null;
  compact_auto?: boolean;
}

/** Умолчание порога сжатия, если ни хост, ни сессия порога не назвали (контракт Ф48-2, D). */
export const COMPACT_DEFAULT_TOKENS = 200_000;

/** Действующий порог: `compact_at` хоста, иначе свой порог сессии, иначе 200 тыс. */
export function compactThreshold(info: CompactInfo): number {
  return info.compact_at ?? info.compact_tokens ?? COMPACT_DEFAULT_TOKENS;
}

/** «контекст 84 из 200 тыс.» + порог (>70 % — жёлтый, >90 % — красный) и «всего $1.20». Ф48-2: с `compact` — «контекст
 * 132 тыс. из 200» (порог сжатия) или «контекст 132 тыс. · без авто-сжатия». */
export function usageLabel(usage: SessionUsage, compact?: CompactInfo): UsageLabel {
  const limit = usage.context_limit;
  if (compact !== undefined && usage.context_tokens !== null && usage.context_tokens !== undefined) {
    const used = usage.context_tokens;
    if (compact.compact_auto === false) {
      const tone: ContextTone = limit !== null && limit > 0 ? (used / limit > 0.9 ? 'danger' : used / limit > 0.7 ? 'warn' : 'normal') : 'normal';
      return { context: `контекст ${used < 1000 ? used : `${thousands(used)} тыс.`} · без авто-сжатия`, contextTone: tone, cost: costOf(usage) };
    }
    const at = compactThreshold(compact);
    const ratio = used / at;
    return {
      context: `контекст ${used < 1000 ? used : `${thousands(used)} тыс.`} из ${thousands(at)}`,
      contextTone: ratio > 0.9 ? 'danger' : ratio > 0.7 ? 'warn' : 'normal',
      cost: costOf(usage),
    };
  }
  if (usage.context_tokens === null || usage.context_tokens === undefined) {
    return { context: CONTEXT_UNKNOWN, contextTone: 'normal', cost: costOf(usage) };
  }
  let context: string;
  let contextTone: ContextTone = 'normal';
  if (limit !== null && limit > 0) {
    context = `контекст ${thousands(usage.context_tokens)} из ${thousands(limit)} тыс.`;
    const ratio = usage.context_tokens / limit;
    if (ratio > 0.9) contextTone = 'danger';
    else if (ratio > 0.7) contextTone = 'warn';
  } else {
    context = usage.context_tokens < 1000 ? `контекст ${usage.context_tokens}` : `контекст ${thousands(usage.context_tokens)} тыс.`;
  }
  return { context, contextTone, cost: costOf(usage) };
}

// --- unified diff ---------------------------------------------------------------------------------------

export type DiffLineKind = 'add' | 'del' | 'hunk' | 'meta' | 'ctx';

export interface DiffLine {
  kind: DiffLineKind;
  text: string;
}

/** Строки diff с видом: «+» зелёным, «−» красным, `@@` серым; заголовки файла (`---`, `+++`, `diff`, `index`) — мета. */
export function parseDiff(patch: string): DiffLine[] {
  const out: DiffLine[] = [];
  let inHunk = false;
  const lines = patch.split('\n');
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  for (const text of lines) {
    if (text.startsWith('@@')) {
      inHunk = true;
      out.push({ kind: 'hunk', text });
    } else if (!inHunk && (text.startsWith('---') || text.startsWith('+++') || text.startsWith('diff ') || text.startsWith('index '))) {
      out.push({ kind: 'meta', text });
    } else if (text.startsWith('+')) {
      out.push({ kind: 'add', text });
    } else if (text.startsWith('-')) {
      out.push({ kind: 'del', text });
    } else {
      out.push({ kind: 'ctx', text });
    }
  }
  return out;
}

/** Размер файла для чипа: «512 Б», «12 КиБ», «1.5 МиБ». */
export function formatBytes(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} МиБ`;
  if (size >= 1024) return `${Math.round(size / 1024)} КиБ`;
  return `${size} Б`;
}

export const FILE_KIND_NOTE = 'модель получит путь к файлу в папке проекта';

/** Строка ленты окна сессии: одно событие или пачка идущих подряд шагов инструментов (свёрнута одной строкой). */
export type FeedRow<E> = { type: 'event'; event: E } | { type: 'tools'; id: number; events: E[]; text: string; errors: number };

/** Подряд идущие `tool`/`tool_done` — одной строкой «3 шага · Read ×2, Bash»; остальное — как есть. Названия
 * инструментов — чужой ввод: только текстом. */
export function groupSteps<E extends { id: number; kind: string; tool?: string | null; is_error?: boolean | null }>(
  events: E[],
): FeedRow<E>[] {
  const rows: FeedRow<E>[] = [];
  let run: E[] = [];
  const flush = (): void => {
    if (run.length === 0) return;
    const calls = run.filter((e) => e.kind === 'tool');
    // Один шаг и так одна свёрнутая строка — обёртка «1 шаг» дала бы лишний клик.
    if (calls.length <= 1) {
      for (const event of run) rows.push({ type: 'event', event });
      run = [];
      return;
    }
    const counts = new Map<string, number>();
    for (const e of calls) counts.set(e.tool ?? '?', (counts.get(e.tool ?? '?') ?? 0) + 1);
    const names = [...counts].map(([name, n]) => (n > 1 ? `${name} ×${n}` : name)).join(', ');
    const errors = run.filter((e) => e.kind === 'tool_done' && e.is_error === true).length;
    const head = stepsLabel(Math.max(calls.length, 1));
    rows.push({ type: 'tools', id: run[0]!.id, events: run, text: names ? `${head} · ${names}` : head, errors });
    run = [];
  };
  for (const event of events) {
    if (event.kind === 'tool' || event.kind === 'tool_done') run.push(event);
    else {
      flush();
      rows.push({ type: 'event', event });
    }
  }
  flush();
  return rows;
}
