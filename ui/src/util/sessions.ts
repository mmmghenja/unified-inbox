// Ф41-2б-2 (`docs/architecture.md`): подписи постоянных сессий хаба — имя, состояние,
// причина ожидания, подсказки модели, команда «Открыть в терминале». Чистые функции без Vue. Название, роль и
// `failure` — чужой ввод (владелец вводит названия, ядро и хост пишут причины): показывать только текстом.
import type {
  EngineName,
  HeldState,
  SessionEngineModel,
  HubSession,
  HubSessionWait,
  ProjectConversation,
  SessionRequest,
} from '../api/types';
import { shellQuote } from './dispatch';
import { engineProfile } from './engineProfiles';
import { heavyCount } from './sessionSub';
import { formatBytes } from './sessionSteps';

export function sessionName(session: Pick<HubSession, 'id' | 'title' | 'role' | 'role_no'>): string {
  const title = session.title?.trim();
  if (title) return title;
  const role = session.role?.trim();
  if (role) return session.role_no !== null && session.role_no > 1 ? `${role} ${session.role_no}` : role;
  return `сессия #${session.id}`;
}

/** Роль с номером («программист», «программист 2») — мелко рядом с названием. */
export function sessionRoleLabel(session: Pick<HubSession, 'role' | 'role_no'>): string {
  const role = session.role?.trim() ?? '';
  return session.role_no !== null && session.role_no > 1 ? `${role} ${session.role_no}` : role;
}

export const SESSION_STATE_LABEL: Record<HeldState, string> = {
  running: 'в работе',
  idle: 'ждёт входа',
  asleep: 'спит',
};

export const SESSION_WAIT_LABEL: Record<HubSessionWait, string> = {
  slot: 'ждёт места',
  elsewhere: 'открыта в другом окне',
  paused: 'пауза лимита подписки',
  heavy: 'ждёт подъёма: контекст большой, кэш остыл',
  compact: 'ждёт конца сжатия',
};

/** Ф48-2 A: строка состояния при прерванной реплике (она ждёт решения владельца). */
export const INTERRUPTED_STATE = 'ждёт тебя: прерванная реплика';

/** Есть ли у сессии прерванные реплики. */
export function hasInterrupted(session: Pick<HubSession, 'interrupted_says' | 'closed_ts'>): boolean {
  return session.closed_ts === null && (session.interrupted_says ?? 0) > 0;
}

/** Строка «Ждёт тебя»: «прерванная реплика — <роль сессии>» (роли нет — название). */
export function interruptedWaitLabel(session: Pick<HubSession, 'id' | 'title' | 'role' | 'role_no'>): string {
  const role = session.role?.trim() ? sessionRoleLabel(session) : sessionName(session);
  return `прерванная реплика — ${role}`;
}

/** Подсказки поля «модель» (список не закрытый — можно вписать своё). Ф74-1: запасные значения — профиль
 * движка из `/status` главнее (`sessionModelHints`). */
export const SESSION_MODEL_HINTS: Record<EngineName, string[]> = {
  claude: ['sonnet', 'opus', 'haiku'],
  opencode: ['deepseek/deepseek-flash', 'anthropic/claude-sonnet-5-5'],
  cursor: ['grok-4.7', 'claude-sonnet-5-5', 'claude-opus-5-5'],
  codex: ['gpt-6.1-sol'],
};

/** «Усилие» по движку: у Claude Code — `claude --effort`, у OpenCode — имя варианта модели, у Cursor — суффикс
 * модели, не ниже high (слово владельца, Ф71-1 П11; «по умолчанию» — high). */
export const SESSION_EFFORTS_BY_ENGINE: Record<EngineName, readonly string[]> = {
  claude: ['low', 'medium', 'high', 'xhigh', 'max'],
  opencode: ['low', 'high', 'max'],
  cursor: ['high', 'xhigh'],
  codex: ['low', 'medium', 'high', 'xhigh', 'max'],
};

/** Ф74-6: живые модели движка из профиля (`models`); нет поля или пусто — пустой список. */
export function sessionModels(engine: string): SessionEngineModel[] {
  const models = engineProfile(engine)?.models;
  return Array.isArray(models) ? models.filter((m) => m && typeof m.id === 'string') : [];
}

/** Подсказки модели по движку: `models` профиля, иначе `model_hints`, иначе запасные. */
export function sessionModelHints(engine: string): string[] {
  const models = sessionModels(engine);
  if (models.length > 0) return models.map((m) => m.id);
  const hints = engineProfile(engine)?.model_hints ?? SESSION_MODEL_HINTS[engine as EngineName];
  return [...(hints ?? [])];
}

/** Уровни «Усилия»: у выбранной модели из `models` — её `efforts`; иначе (модели нет, не из списка, поля `models`
 * нет) — `efforts` профиля, иначе запасные. */
export function sessionEfforts(engine: string, model?: string | null): string[] {
  const picked = model ? sessionModels(engine).find((m) => m.id === model.trim()) : undefined;
  if (picked && Array.isArray(picked.efforts) && picked.efforts.length > 0) return [...picked.efforts];
  const efforts = engineProfile(engine)?.efforts ?? SESSION_EFFORTS_BY_ENGINE[engine as EngineName];
  return [...(efforts ?? [])];
}

/** Умолчание усилия выбранной модели (`default_effort`), если оно в её списке. */
export function modelDefaultEffort(engine: string, model?: string | null): string | null {
  const picked = model ? sessionModels(engine).find((m) => m.id === model.trim()) : undefined;
  const value = picked?.default_effort;
  return value && sessionEfforts(engine, model).includes(value) ? value : null;
}

/** Пункты списка «Усилие»: уровни движка (модели); сохранённое значение не из списка (старые данные) — отдельным
 * пунктом, только если `keepCurrent` (по умолчанию да: так показывается уже сохранённое). */
export function effortChoices(engine: string, current?: string | null, model?: string | null, keepCurrent = true): string[] {
  const list = sessionEfforts(engine, model);
  if (keepCurrent && current && !list.includes(current)) list.push(current);
  return list;
}

/** Значение усилия после смены движка или модели в форме: нет в новом списке — «по умолчанию» (пустая строка). */
export function effortForEngine(engine: string, value: string, model?: string | null): string {
  return sessionEfforts(engine, model).includes(value) ? value : '';
}

/** Текст чипа шапки сессии: «<модель> · <усилие>»; модели нет — «модель по умолчанию», усилия нет — без второй части. */
export function modelChipText(session: Pick<HubSession, 'model' | 'effort'>): string {
  const model = session.model?.trim() ? session.model.trim() : 'модель';
  const effort = session.effort?.trim();
  if (effort) return `${model} · ${effort}`;
  return session.model?.trim() ? model : 'модель по умолчанию';
}

export const SESSION_CLOSE_CONFIRM = 'Сессия закроется насовсем: подписка и ждущее пропадут';

export const SESSION_TERMINAL_NOTE = 'Сначала усыпи сессию: одна сессия — одно окно';

/** Команда продолжить спящую сессию руками: у Claude Code — `claude --resume`, у OpenCode — `opencode -s`
 * (флаг проверен по `opencode --help`), у Cursor — `cursor-agent --resume <chatId>`, у Codex — `codex resume <id>`. `null`, пока нет папки или id разговора. */
export function sessionTerminalCommand(
  session: Pick<HubSession, 'engine' | 'state' | 'engine_session_id' | 'workdir'> & { subdir?: string | null; cwd?: string },
): string | null {
  if (session.state !== 'asleep' || !session.engine_session_id || !session.workdir) return null;
  const flag = { claude: 'claude --resume', opencode: 'opencode -s', cursor: 'cursor-agent --resume', codex: 'codex resume' }[session.engine];
  // Ф50-7: папка процесса — `cwd` ядра; нет — рабочая папка проекта плюс подпапка.
  const dir = session.cwd ? session.cwd : session.subdir ? `${session.workdir}/${session.subdir}` : session.workdir;
  return `cd ${shellQuote(dir)} && ${flag} ${shellQuote(session.engine_session_id)}`;
}

/** Ждущие карточки сессий проекта: загруженный список запросов, пока он не загружен — `pending_requests` записи.
 * Закрытые не считаются. */
export function sessionWaitCount(
  sessions: { items: Record<number, HubSession>; pending: Record<number, SessionRequest[]> },
  projectId: number,
): number {
  let sum = 0;
  for (const s of Object.values(sessions.items)) {
    if (s.project_id !== projectId || s.closed_ts !== null) continue;
    sum += sessions.pending[s.id]?.length ?? s.pending_requests;
    if (heavyCount(s) > 0) sum += 1; // Ф41-4: тяжёлая сессия с ждущими сигналами — одна строка «ждёт подъёма»
    if (hasInterrupted(s)) sum += 1; // Ф48-2: прерванная реплика — одна строка «ждёт решения»
  }
  return sum;
}

/** Подпись ждущей карточки: «<сессия>: просит <tool>» / «<сессия>: вопрос». */
export function sessionRequestLabel(
  session: Pick<HubSession, 'id' | 'title' | 'role' | 'role_no'>,
  request: { kind: 'permission' | 'question'; tool: string | null } | null,
): string {
  const name = sessionName(session);
  if (request === null) return `${name}: ждёт решения`;
  return request.kind === 'question' ? `${name}: вопрос` : `${name}: просит ${request.tool ?? '?'}`;
}

/** Роль каталога, которой соответствует роль сессии: по `names` без учёта регистра, номер линии не в счёт. */
export function catalogRoleFor<T extends { names: string[] }>(role: string | null | undefined, items: T[]): T | null {
  const name = role?.trim().toLowerCase() ?? '';
  if (name === '') return null;
  return items.find((r) => r.names.some((n) => n.trim().toLowerCase() === name)) ?? null;
}

const SUBDIR_MAX = 24;

/** Ф50-7: подпапка для узких мест — длинная обрезается с начала («…/zakaz-17»), полный путь кладут в `title`. */
export function subdirLabel(subdir: string): string {
  if (subdir.length <= SUBDIR_MAX) return subdir;
  const tail = subdir.slice(-(SUBDIR_MAX - 2));
  const slash = tail.indexOf('/');
  return `…/${slash >= 0 ? tail.slice(slash + 1) : tail}`;
}

const CONV_PREVIEW_MAX = 70;

/** Ф99-1: строка прошлого разговора Claude Code в списке «Продолжить разговор»: время последней записи, начало
 * первой реплики (чужой ввод — только текстом), размер; занятый — с номером сессии хаба. */
export function conversationLabel(conv: ProjectConversation, now: number = Date.now()): string {
  const at = new Date(conv.last_ts);
  const sameYear = at.getFullYear() === new Date(now).getFullYear();
  const pad = (n: number) => String(n).padStart(2, '0');
  const day = `${pad(at.getDate())}.${pad(at.getMonth() + 1)}${sameYear ? '' : `.${at.getFullYear()}`}`;
  const stamp = `${day} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
  const text = conv.preview.trim();
  const preview = text === ''
    ? 'без реплик'
    : `«${text.length > CONV_PREVIEW_MAX ? `${text.slice(0, CONV_PREVIEW_MAX - 1)}…` : text}»`;
  const busy = conv.hub_session_id !== null ? ` — ведёт сессия #${conv.hub_session_id}` : '';
  return `${stamp} · ${preview} · ${formatBytes(conv.size)}${busy}`;
}
