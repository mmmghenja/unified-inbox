// Единственный модуль API-клиента (спека, «Уточнения архитектора», раздел «API-клиент»).
// `credentials: 'same-origin'`, на не-GET — `X-Hub-CSRF` и `X-Hub-Client: hub-ui`, `idempotency_key` —
// `newId()` (`util/ids.ts`; Ф10-7: без безопасного контекста `crypto.randomUUID` нет), повтор при сетевой ошибке
// — с тем же ключом.
//
// CSRF хранится ТОЛЬКО в памяти этого модуля (переменная модуля, никакого браузерного хранилища или куки —
// см. `ui/README.md` и тест-сторож `tests/11-guard.spec.ts`). После перезагрузки страницы он восстанавливается
// вызовом `authMe()` (`GET /auth/me`, спека «Вход»).

import type {
  AgentRun,
  MyDataField,
  MyDataFields,
  MyDataOverview,
  ApproveBatchResult,
  AttentionMode,
  AuthMe,
  Channel,
  ChannelNewResult,
  EngineName,
  EngineSetResult,
  EngineStatus,
  AutosendResult,
  HomeReactItem,
  Interview,
  InterviewCurrent,
  InterviewHintResult,
  InterviewLine,
  InboxItem,
  JoinRequest,
  RuleRequest,
  PeerMsg,
  PeerSendResult,
  Reminder,
  GroupMembers,
  GroupOp,
  GroupOpInput,
  LeaveRequest,
  MapOptions,
  MapPreview,
  InboundRule,
  InboundRuleInput,
  InboundRulesList,
  MapRule,
  MapRuleAdded,
  MapRuleDeleted,
  MapRuleInput,
  MentionPerson,
  MediaFetchResponse,
  MessageEditsResponse,
  MessageOp,
  MessagePage,
  MoveTarget,
  NewChannelTo,
  OutboxItem,
  OutboxListItem,
  OutboxListStatus,
  Machine,
  MachineList,
  PeopleList,
  PersonCard,
  PersonSuggestion,
  Project,
  ProjectSub,
  CardRoute,
  ResolveCandidate,
  RecipientHit,
  RemoteStatus,
  ResolveUnknownDecision,
  SearchResult,
  SendResult,
  SessionResponse,
  StatusResponse,
  SubsResponse,
  OfflineLine,
  SessionsToday,
  AgentRequest,
  AgentRequestApproveBody,
  ButtonPress,
  ButtonPressReply,
  HumanRequest,
  OwnerAgentSettings,
  OwnerProjectModel,
  PatternsState,
  PatternsPut,
  AgentDecision,
  OwnerAnswerBody,
  OwnerClosedRow,
  OwnerErrand,
  OwnerQueue,
  OwnerTalkBranch,
  OwnerTalkMsg,
  AutopilotState,
  HandoffInput,
  RoleInfo,
  RoleText,
  SessionRoleText,
  MdFile,
  SyncResponse,
  UploadResult,
  ThreadListItem,
  ThreadMessagePage,
  UiSession,
  HubSession,
  SessionCreateInput,
  ProjectConversation,
  SessionDetail,
  SessionEvent,
  SessionEventFull,
  TermInfo,
  TermRunRecord,
  TermRunSource,
  SessionFileResult,
  SessionWantSub,
  SubTranscript,
} from './types';
import { newId } from '../util/ids';

const API_BASE = '/api/v1';
const CLIENT_NAME = 'hub-ui';

let csrf: string | null = null;

export function setCsrf(value: string | null): void {
  csrf = value;
}

export function getCsrf(): string | null {
  return csrf;
}

/** Вызывается один раз при 401 на любом запросе — экран «сессия истекла» (спека, «Вход UI»). */
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export class NetworkError extends Error {
  /** Исходная ошибка `fetch` (в lib целевой версии `Error.cause` нет — поле объявлено здесь). */
  readonly cause: unknown;

  constructor(cause: unknown) {
    super('network error');
    this.name = 'NetworkError';
    this.cause = cause;
  }
}

const SAFE_METHODS = new Set(['GET', 'HEAD']);

/** Ф8-1 A1: таймаут обычного запроса API (SSE — `EventSource` — сюда не относится и не ограничивается). */
export const REQUEST_TIMEOUT_MS = 20_000;

/** 401 на `/auth/session` — обычный исход самой ручки (неверный/просроченный одноразовый код), а не «была
 * сессия и протухла»: вызывающий (`login()`) сам решает, что показать, глобальный обработчик здесь ни при
 * чём (F10, приёмка 6b). В отличие от `/auth/me`, это верно для ЛЮБОГО вызова этого пути, без исключений. */
// Ф10-7 П7: то же у входа паролем — 401 там «неверный пароль», а не «сессия истекла».
const UNAUTHORIZED_EXEMPT_PATHS = new Set(['/auth/session', '/auth/password']);

interface RequestOptions {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  /** Не звать глобальный `unauthorizedHandler` на 401 ЭТОГО конкретного вызова (не пути вообще). Нужно
   * стартовому `authMe()` в `bootstrap()`: 401 там значит «сессии ещё не было», а не «истекла», и результат
   * уже обрабатывает сам вызывающий код (F10) — но повторный `authMe()` из F2 (проверка при реконнекте SSE)
   * этот флаг не ставит: там 401 обязан переключить экран через обычный механизм. */
  silent401?: boolean;
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = new URL(API_BASE + path, window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null) {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.pathname + url.search;
}

/** Ф38-4: устаревшая вкладка. `csrf` считается из значения cookie, а cookie у всех вкладок общая: вход в другой
 * вкладке (второе окно `hub ui`, вход паролем) меняет cookie, и у этой вкладки в памяти остаётся csrf прежней —
 * читать она продолжает, а на любую запись middleware ядра отвечает пустым 403 (отказ самой ручки — 403 с
 * `detail`, его не трогаем). Тогда csrf перечитывается тем же `GET /auth/me`, что при загрузке страницы; сменился
 * — запрос повторяется ОДИН раз. Повтор безопасен: первый запрос до ручки не дошёл. Чужой сайт так не может —
 * `/auth/me` ему не прочитать (same-origin). */
let csrfRefresh: Promise<void> | null = null;

function refreshCsrf(): Promise<void> {
  // Одно перечитывание на пачку одновременно упавших записей; 401 — обычный путь «сессия истекла».
  csrfRefresh ??= authMe()
    .then(
      () => undefined,
      () => undefined,
    )
    .finally(() => {
      csrfRefresh = null;
    });
  return csrfRefresh;
}

/** `used` — csrf, с которым ушёл упавший запрос. `true` — csrf с тех пор сменился, запрос стоит повторить. */
async function csrfWentStale(err: unknown, method: string, used: string | null): Promise<boolean> {
  if (SAFE_METHODS.has(method)) return false;
  if (!(err instanceof ApiError) || err.status !== 403 || err.body !== null) return false;
  if (csrf === used) await refreshCsrf();
  return csrf !== null && csrf !== used;
}

async function rawRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const used = csrf;
  try {
    return await rawAttempt<T>(path, opts);
  } catch (err) {
    if (!(await csrfWentStale(err, opts.method ?? 'GET', used))) throw err;
    return rawAttempt<T>(path, opts);
  }
}

async function rawAttempt<T>(path: string, opts: RequestOptions): Promise<T> {
  const method = opts.method ?? 'GET';
  const headers: Record<string, string> = { 'X-Hub-Client': CLIENT_NAME };
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  if (!SAFE_METHODS.has(method)) {
    headers['X-Hub-CSRF'] = csrf ?? '';
  }

  // Ф8-1 A1: у `fetch` нет своего таймаута — повисший запрос держал `syncing=true` в сторе навсегда, лента
  // стояла до перезагрузки. Через `REQUEST_TIMEOUT_MS` запрос отменяется (`AbortController`) и вызывающий
  // получает `NetworkError` — тот же тип, что сетевой сбой. Гонка с таймером, а не только `signal`: ожидание не
  // должно зависеть от того, уважает ли конкретный `fetch` отмену. Таймер покрывает и чтение тела ответа.
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new NetworkError(new Error(`timeout ${REQUEST_TIMEOUT_MS} ms`)));
    }, REQUEST_TIMEOUT_MS);
  });
  try {
    return await Promise.race([sendRequest<T>(path, opts, method, headers, body, controller.signal), timeout]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

async function sendRequest<T>(
  path: string,
  opts: RequestOptions,
  method: string,
  headers: Record<string, string>,
  body: BodyInit | undefined,
  signal: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      body,
      credentials: 'same-origin',
      signal,
    });
  } catch (cause) {
    throw new NetworkError(cause);
  }

  // F10 (приёмка 6b): 401 на `/auth/session` (неверный/просроченный код входа) — ОЖИДАЕМЫЙ обычный исход
  // этой ручки, не признак «сессия была и протухла»; и 401 на стартовом (не силентном) `authMe()` из
  // `bootstrap()` — тоже не «протухла», а «ещё не было». Раньше здесь безусловно дёргался общий обработчик,
  // и экран на секунду мигал «сессия истекла» вместо экрана входа с кодом/логина.
  // Ф10-7 (приёмка): 421 — то же, что 401. Удалённый слушатель отвечает 421 на всё, когда доступ с телефона
  // выключен на ноутбуке (стоп-кран «потерял телефон»): без этого страница держала бы уже загруженную
  // переписку на экране. Локальному UI 421 не приходит никогда (Host у него всегда верный).
  const lostSession = response.status === 401 || response.status === 421;
  if (lostSession && !UNAUTHORIZED_EXEMPT_PATHS.has(path) && !opts.silent401) {
    unauthorizedHandler?.();
  }

  const contentType = response.headers.get('content-type') ?? '';
  const payload = contentType.includes('application/json') ? await response.json().catch(() => null) : null;

  if (!response.ok) {
    const message =
      payload && typeof payload === 'object' && payload !== null && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : response.statusText;
    throw new ApiError(response.status, message, payload);
  }

  return payload as T;
}

function get<T>(path: string, query?: RequestOptions['query'], opts: { silent401?: boolean } = {}): Promise<T> {
  return rawRequest<T>(path, { method: 'GET', query, silent401: opts.silent401 });
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return rawRequest<T>(path, { method: 'POST', body });
}

function patchJson<T>(path: string, body: unknown): Promise<T> {
  return rawRequest<T>(path, { method: 'PATCH', body });
}

function putJson<T>(path: string, body: unknown): Promise<T> {
  return rawRequest<T>(path, { method: 'PUT', body });
}

function del(path: string): Promise<null> {
  return rawRequest<null>(path, { method: 'DELETE' });
}

// --- Вход -------------------------------------------------------------------------------------------------

export async function login(code: string): Promise<SessionResponse> {
  // `POST /auth/session` освобождён сервером от CSRF (см. middleware.py, `_CSRF_EXEMPT_PATHS`) — заголовок
  // `X-Hub-CSRF` всё равно уходит (пустым, csrf ещё нет), сервер его для этого пути не проверяет.
  const result = await postJson<SessionResponse>('/auth/session', { code });
  setCsrf(result.csrf);
  return result;
}

export async function authMe(opts: { silent401?: boolean } = {}): Promise<AuthMe> {
  const me = await get<AuthMe>('/auth/me', undefined, opts);
  setCsrf(me.csrf);
  return me;
}

export async function logout(): Promise<{ ok: boolean }> {
  const result = await postJson<{ ok: boolean }>('/auth/logout', {});
  setCsrf(null);
  return result;
}

// --- Ф10-7: вход с телефона, продление, список входов, доступ с телефона ------------------------------------

/** `POST /auth/password` — только на удалённом слушателе, без CSRF (Origin браузер шлёт сам). 401 — неверный
 * пароль (глобальный обработчик не зовётся, П7), 429 — блок (`body.retry_after_s`), 403/421 — выключено. */
export async function loginWithPassword(password: string): Promise<SessionResponse> {
  const result = await postJson<SessionResponse>('/auth/password', { password });
  setCsrf(result.csrf);
  return result;
}

/** `POST /auth/renew` — продлить свою сессию на 30 дней (кука выставляется заново). */
export function renewSession() {
  return postJson<{ expires_ts: number }>('/auth/renew', {});
}

export function listSessions() {
  return get<{ sessions: UiSession[] }>('/auth/sessions');
}

export function revokeSession(sessionId: number) {
  return postJson<{ ok: boolean }>(`/auth/sessions/${sessionId}/revoke`, {});
}

export function revokeAllSessions(keepCurrent: boolean) {
  return postJson<{ revoked: number }>('/auth/sessions/revoke-all', { keep_current: keepCurrent });
}

export function getRemote() {
  return get<RemoteStatus>('/remote');
}

/** Только локальный слушатель (на удалённом — 403). 422 — нельзя включить без пароля/адреса, плохой адрес. */
export function putRemote(fields: { enabled?: boolean; hosts?: string[] }) {
  return putJson<RemoteStatus>('/remote', fields);
}

/** Только локальный; 10–200 символов, иначе 422. Смена отзывает все удалённые сессии. */
export function setRemotePassword(password: string) {
  return putJson<{ ok: boolean; revoked_remote: number }>('/remote/password', { password });
}

// --- Ф16-1: движок сессий (Claude Code / OpenCode) -----------------------------------------------------------

/** Ф41-6: раскладка «Главной» (`GET/PUT /ui/layout`). Ручки может не быть (404) — вызывающий работает в памяти. */
export function getUiLayout() {
  return get<{ layout: unknown }>('/ui/layout');
}

export function putUiLayout(layout: unknown) {
  return putJson<{ layout: unknown }>('/ui/layout', { layout });
}

export function getEngine() {
  return get<EngineStatus>('/engine');
}

/** Только UI-сессия (с ноутбука и с телефона; Ф78-2). Модель и усилие не переданы — прежний выбор движка,
 * `null` — умолчание. Движок недоступен — 409 с причиной ядра. */
export function putEngine(engine: EngineName, choice: { model?: string | null; effort?: string | null } = {}) {
  return putJson<EngineSetResult>('/engine', { engine, ...choice });
}

/** Ф78-2d: свой выбор службы фактов; `engine: null` — снова за агентом. */
export function putEngineFacts(
  engine: EngineName | null,
  choice: { model?: string | null; effort?: string | null } = {},
) {
  return putJson<EngineStatus>('/engine/facts', { engine, ...choice });
}

/** Ф83: запасной агента/фактов (только кука); `engine: null` — запасного нет. 409 — недоступен, 422 — у фактов тот же
 * движок, у агента (Ф85-1) — совпадает с основным целиком (тот же движок с другой моделью — можно). */
export function putEngineFallback(
  who: 'agent' | 'facts',
  engine: EngineName | null,
  choice: { model?: string | null; effort?: string | null } = {},
) {
  return putJson<EngineStatus>(`/engine/fallback/${who}`, { engine, ...choice });
}

/** Ф83: «Вернуть на основной» агента/фактов. */
export function returnEngineFallback(who: 'agent' | 'facts') {
  return postJson<EngineStatus & { returned: boolean }>(`/engine/fallback/${who}/return`, {});
}

// --- Чтение -------------------------------------------------------------------------------------------------

/** Ф99-7.3: хвост стенограммы внешней сессии Claude Code (лента «только смотреть»). */
export function getSubTranscript(subId: number, limit = 100) {
  return get<SubTranscript>(`/subs/${subId}/transcript`, { limit });
}

export function listProjects() {
  return get<Project[]>('/projects');
}

export function listChannels(projectId: number) {
  return get<Channel[]>(`/projects/${projectId}/channels`);
}

export function listMessages(
  channelId: number,
  opts: { before?: string; limit?: number } = {},
): Promise<MessagePage> {
  return get<MessagePage>(`/channels/${channelId}/messages`, { before: opts.before, limit: opts.limit });
}

export function listInbox(opts: { project?: number; tier?: string } = {}) {
  return get<InboxItem[]>('/inbox', { project: opts.project, tier: opts.tier });
}

/** Спека 2b/2e, раздел E: ответы треда страницами, тем же курсором, что лента канала; родитель — отдельным
 * полем `parent`. `thread_ext_id` — произвольная строка источника (у Slack это `ts`, содержит точку) —
 * обязательно кодируется в URL, в отличие от числового `channelId`. */
export function listThreadMessages(
  channelId: number,
  threadExtId: string,
  opts: { before?: string; limit?: number } = {},
): Promise<ThreadMessagePage> {
  return get<ThreadMessagePage>(
    `/channels/${channelId}/threads/${encodeURIComponent(threadExtId)}/messages`,
    { before: opts.before, limit: opts.limit },
  );
}

/** Спека 2b/2e, раздел E: треды канала (кнопка «Треды» в шапке — подписанные треды с непрочитанным, свежие
 * первыми), нужна, когда подписанный тред есть, а родителя в открытой ленте нет. */
export function listChannelThreads(channelId: number, opts: { unread?: boolean; limit?: number } = {}) {
  return get<ThreadListItem[]>(`/channels/${channelId}/threads`, {
    unread: opts.unread ? 1 : undefined,
    limit: opts.limit,
  });
}

export function search(q: string, project?: number) {
  return get<SearchResult[]>('/search', { q, project });
}

export function getOutbox(outboxId: number) {
  return get<OutboxItem>(`/outbox/${outboxId}`);
}

/** `GET /outbox?status=&limit=` (F8, добавлено архитектором) — список очереди одного статуса, новые
 * сверху. Панель очереди в `TopBar`/`OutboxQueuePanel.vue` перезапрашивает это после каждой применённой
 * пачки `/sync`, пока открыта. */
export function listOutbox(status: OutboxListStatus, limit = 100, since?: number) {
  return get<OutboxListItem[]>('/outbox', { status, limit, since });
}

/** Ф8-6 A3: «Нужна реакция» — беседы, где ждут реакции, по всем проектам. */
export function getHomeReact() {
  return get<HomeReactItem[]>('/home/react');
}

/** Ф37-1 A2: разрешить/запретить беседе отправку без подтверждения для роли (только UI; 422 с текстом в `detail`). */
export function putChannelAutosend(channelId: number, role: string, on: boolean) {
  return putJson<AutosendResult>(`/channels/${channelId}/autosend`, { role, on });
}

/** Ф14-5 П9: живые подписки сессий (на проекты и беседы). */
export function listSubs() {
  return get<SubsResponse>('/subs', { live: 1 });
}

/** Ф8-6 A1: запуски Claude, новые сверху. */
export function listAgentRuns(opts: { since?: number; project?: number; limit?: number } = {}) {
  return get<AgentRun[]>('/agent/runs', { since: opts.since, project: opts.project, limit: opts.limit });
}

export function getStatus() {
  return get<StatusResponse>('/status');
}

export function sync(since: number, limit = 200) {
  return get<SyncResponse>('/sync', { since, limit });
}

export function resolveChannels(query: string, project?: number) {
  return postJson<ResolveCandidate[]>('/channels/resolve', { query, project: project ?? null });
}

export function streamUrl(): string {
  return API_BASE + '/stream';
}

// --- Запись -------------------------------------------------------------------------------------------------

export interface SendMessageInput {
  channelId: number;
  text: string;
  replyTo?: string | null;
  thread?: string | null;
  idempotencyKey?: string;
  /** Ф10-4 A3 + Ф11-3b A2: до 10 `upload_id` без повторов — одно сообщение; `text` при них может быть пустым. */
  files?: string[];
}

const SEND_RETRY_ATTEMPTS = 2; // первая попытка + один повтор при сетевой ошибке, тем же idempotency_key

/** `send` — единственная ручка, для которой спека явно требует повтор при сетевой ошибке с тем же ключом
 * идемпотентности (тест 9). Ключ фиксируется ДО первой попытки и не меняется между повторами. */
export async function sendMessage(input: SendMessageInput): Promise<SendResult> {
  const idempotencyKey = input.idempotencyKey ?? newId();
  const body = {
    channel_id: input.channelId,
    text: input.text,
    reply_to: input.replyTo ?? null,
    thread: input.thread ?? null,
    idempotency_key: idempotencyKey,
    ...(input.files !== undefined && input.files.length > 0 ? { files: input.files } : {}),
  };

  let lastError: unknown;
  for (let attempt = 0; attempt < SEND_RETRY_ATTEMPTS; attempt++) {
    try {
      return await postJson<SendResult>('/messages/send', body);
    } catch (err) {
      lastError = err;
      if (!(err instanceof NetworkError)) {
        throw err; // ошибка HTTP (409/404/422/…) — не сетевая, повторять её тем же ключом бессмысленно
      }
    }
  }
  throw lastError;
}

/** Ф10-4 A2: срок загрузки файла (до 50 МиБ) — не обычные 20 с API. */
export const UPLOAD_TIMEOUT_MS = 300_000;

/** Ф10-4 A2: `POST /uploads?channel_id=` — сырые байты файла телом; имя — `X-Filename` (urlencoded UTF-8),
 * `Content-Length` браузер ставит сам по `Blob`. Тип файла ядро определяет по сигнатуре, `Content-Type` —
 * только для порядка. Повтора нет: загрузка без ключа, повтор дал бы вторую строку (её уберёт очистка).
 * Ф14-4 D4: `opts.voice` — голосовое из записи (`&voice=1`: ядро примет только webm/ogg до 20 МиБ).
 * Ф38-4: единственный повтор — после пустого 403 с устаревшим csrf (`csrfWentStale`): до ручки тот запрос не дошёл. */
export async function uploadFile(
  channelId: number,
  file: Blob,
  name: string,
  opts: { voice?: boolean } = {},
): Promise<UploadResult> {
  const used = csrf;
  const headers = (): Record<string, string> => ({
    'X-Hub-Client': CLIENT_NAME,
    'X-Hub-CSRF': csrf ?? '',
    'Content-Type': 'application/octet-stream',
    'X-Filename': encodeURIComponent(name),
  });
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new NetworkError(new Error(`timeout ${UPLOAD_TIMEOUT_MS} ms`)));
    }, UPLOAD_TIMEOUT_MS);
  });
  const path = `/uploads?channel_id=${channelId}${opts.voice ? '&voice=1' : ''}`;
  const attempt = () => sendRequest<UploadResult>(path, {}, 'POST', headers(), file, controller.signal);
  try {
    return await Promise.race([
      attempt().catch(async (err: unknown) => {
        if (!(await csrfWentStale(err, 'POST', used))) throw err;
        return attempt();
      }),
      timeout,
    ]);
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

/** Ф59-1: диктовка (Yandex SpeechKit через ядро). `enabled=false` — ключ не задан, кнопок диктовки нет. */
export function getDictateStatus() {
  return get<{ enabled: boolean }>('/dictate');
}

/** Ф59-1: один кусок LPCM 16 кГц Int16LE моно (≤ 30 с, ≤ 1 МиБ) -> `{text}`. Таймаут и отмену держит вызывающий
 * (`signal`); повтора нет, кроме единственного после пустого 403 с устаревшим csrf (до ручки тот запрос не дошёл).
 * Ошибки ядра: 404 `dictate_off`, 400 `bad_audio`, 413, 429 (`dictate_daily` — суточный лимит), 502 (`dictate_key`),
 * 504 — `ApiError.message` = `detail`. */
export async function postDictate(bytes: Uint8Array, signal: AbortSignal): Promise<{ text: string }> {
  const used = csrf;
  const headers = (): Record<string, string> => ({
    'X-Hub-Client': CLIENT_NAME,
    'X-Hub-CSRF': csrf ?? '',
    'Content-Type': 'application/octet-stream',
  });
  const attempt = () =>
    sendRequest<{ text: string }>('/dictate', {}, 'POST', headers(), bytes as unknown as BodyInit, signal);
  try {
    return await attempt();
  } catch (err) {
    if (!(await csrfWentStale(err, 'POST', used))) throw err;
    return attempt();
  }
}

/** Ф69-1: «Снять стоп» аккаунта (PeerFlood). 409 `nothing_to_resume` — снимать нечего. */
export function resumeAccount(accountId: number) {
  return postJson<{ ok: boolean; cleared: string[] }>(`/accounts/${accountId}/resume`, {});
}

/** Ф66-3 F: «Снять паузу лимита» — ядро пишет отметку, хост убирает паузы лимита и работы сверх подписки. */
export function clearWorkPause() {
  return postJson<{ ok: boolean; cleared_ts: number }>('/work/pause/clear', {});
}

export function cancelOutbox(outboxId: number) {
  return postJson<{ status: string }>(`/outbox/${outboxId}/cancel`, {});
}

/** 24.10: «Отправить сейчас» у подтверждённой строки в очереди (по графику или к сроку) — уходит при ближайшей
 * выдаче. 409 `not_waiting` — уже уходит, уже ушло или не в очереди (`detail` — причина). */
export function sendOutboxNow(outboxId: number) {
  return postJson<{ status: string }>(`/outbox/${outboxId}/send-now`, {});
}

/** Ф30-1: `sendNow` — «Отправить сейчас» черновика, чьё время `send_at` прошло (409 `send_at_passed`). */
export function approveOutbox(outboxId: number, text?: string, sendNow = false) {
  return postJson<{ status: string; paced?: boolean; not_before_ts?: number | null; send_at_ts?: number }>(
    `/outbox/${outboxId}/approve`,
    sendNow ? { text: text ?? null, send_now: true } : { text: text ?? null },
  );
}

/** Ф12-2 A4: подтверждение пачкой (≤ 50, без правки текста); слоты графика — по порядку `ids`. */
export function approveOutboxBatch(ids: number[]) {
  return postJson<{ results: ApproveBatchResult[] }>('/outbox/approve-batch', { ids });
}

export function resolveUnknownOutbox(outboxId: number, decision: ResolveUnknownDecision) {
  return postJson<{ status: string }>(`/outbox/${outboxId}/resolve-unknown`, { decision });
}

export function markChannelRead(channelId: number, upToMessageId: number) {
  return postJson<{ changed: boolean }>(`/channels/${channelId}/read`, { up_to_message_id: upToMessageId });
}

/** Ф8-5 A2.3: «Загрузить» вложение (только UI-сессия). Ответ — обновлённый `media` сообщения (П8). */
export function fetchMedia(messageId: number, idx: number) {
  return postJson<MediaFetchResponse>(`/messages/${messageId}/media/${idx}/fetch`, {});
}

/** Ф14-6: кнопка «Расшифровать» / «Расшифровать заново» — только UI-сессия (CSRF — как у всех POST). */
export function transcribeMedia(mediaId: number) {
  return postJson<MediaFetchResponse>(`/media/${mediaId}/transcribe`, {});
}

/** Ф8-4 B2: удалить сообщение у себя в хабе (только UI-сессия; у источника оно остаётся). 204 — и для уже
 * удалённого; 409 — своё исходящее ещё отправляется. */
export function hideMessage(messageId: number) {
  return del(`/messages/${messageId}`);
}

/** Ф10-3 П1: одна операция реакции владельца (только UI-сессия). Ставит в очередь ядра — правду о реакциях потом
 * пришлёт источник событием (`reactions_json` через `/sync`); `off` — реакции выключены у аккаунта. */
export function setReaction(messageId: number, emoji: string, on: boolean) {
  return postJson<{ status: 'queued' | 'off' }>(`/messages/${messageId}/reactions`, { emoji, on });
}

/** Ф14-8 A3: правка своего сообщения у источника (только UI-сессия) — в очередь ядра; `unchanged` — текст тот же,
 * ничего не записано. Правду (новый текст) пришлёт источник через `/sync`. */
export function editMessage(messageId: number, text: string) {
  return postJson<{ status: 'queued' | 'unchanged'; op: MessageOp | null }>(`/messages/${messageId}/edit`, { text });
}

/** Ф14-8 A3: «удалить у всех» — 1..10 id одной беседы (части альбома), всё-или-ничего. */
export function revokeMessages(messageIds: number[]) {
  return postJson<{ status: 'queued'; ops: Record<string, MessageOp | null> }>('/messages/revoke', {
    message_ids: messageIds,
  });
}

/** Ф14-8 A3: снять ждущую или неудавшуюся операцию (409 `in_flight` — уже отправляется). */
export function cancelMessageOp(messageId: number) {
  return del(`/messages/${messageId}/op`);
}

/** Ф8-4 B3: удалить беседу у себя в хабе целиком (вернётся с первым новым сообщением). */
export function hideChannel(channelId: number) {
  return del(`/channels/${channelId}`);
}

export interface ChannelNewInput {
  accountId: number;
  /** Ф14-2 A2: `null` — «по правилам» (заготовку раскладывают правила). */
  projectId: number | null;
  /** Строка — Telegram `@username` / адрес почты; объект — `tg_user` / `slack_users` (Ф14-2 A2). */
  to: NewChannelTo;
  /** Только почта (обязательна); у Telegram и Slack не передаётся. */
  subject?: string | null;
}

/** Ф14-2 A5: «Кому» формы «Новое сообщение» — беседы и люди по подстроке (только UI-сессия). */
export function searchRecipients(q: string) {
  return get<RecipientHit[]>('/recipients', { q });
}

/** Ф27-1 B: подсказка `@` в беседе Slack — до 8 людей воркспейса (сначала участники беседы). */
export function searchMentionPeople(accountId: number, q: string, channelId: number) {
  return get<MentionPerson[]>(`/accounts/${accountId}/mention_people`, { q, channel_id: channelId });
}

/** Ф41-6: запись беседы целиком (столбец доски после перезагрузки страницы). */
export function getChannel(channelId: number) {
  return get<Channel>(`/channels/${channelId}`);
}

/** Ф27-1 C: `account_id` беседы (в списке бесед UI его нет) — нужен для подсказки `@`. */
export function getChannelAccountId(channelId: number) {
  return get<{ account_id: number }>(`/channels/${channelId}`).then((c) => c.account_id);
}

/** Ф12-5 A1: «Новое сообщение» — пустая беседа-заготовка новому адресату (только UI-сессия). Повтора нет: ядро
 * само отдаёт уже открытую тому же адресату (`created: false`). */
export function createChannel(input: ChannelNewInput) {
  return postJson<ChannelNewResult>('/channels/new', {
    account_id: input.accountId,
    project_id: input.projectId,
    to: input.to,
    ...(input.subject !== undefined && input.subject !== null ? { subject: input.subject } : {}),
  });
}

/** Ф8-4 C3: прежние версии правленого сообщения, от старой к новой. */
export function listMessageEdits(messageId: number) {
  return get<MessageEditsResponse>(`/messages/${messageId}/edits`);
}

/** Ф8-5 A3: адрес файла — тот же origin, cookie-сессия; тип и `inline`/`attachment` решает сервер. */
export function mediaUrl(mediaId: number): string {
  return `${API_BASE}/media/${mediaId}`;
}

/** Спека 2b/2e, раздел E: маркер прочтения ТРЕДА (не канала), только UI-сессия, только вперёд. */
export function markThreadRead(channelId: number, threadExtId: string, upToMessageId: number) {
  return postJson<{ changed: boolean }>(
    `/channels/${channelId}/threads/${encodeURIComponent(threadExtId)}/read`,
    { up_to_message_id: upToMessageId },
  );
}

export function patchChannel(
  channelId: number,
  fields: {
    project_id?: number | null;
    muted?: boolean;
    archived?: boolean;
    // Ф8-3 A6: ручной режим внимания; `null` — «авто» (по политике проекта). Только UI-сессия.
    attention?: AttentionMode | null;
    // Ф14-1 C1: назад к правилам (ядро пересчитывает проект беседы; топики — каскадом).
    map_source?: 'auto';
    // Ф34-1: стадия беседы (`null`/`""` — снять) и заметка к ней; не переданная заметка при смене стадии стирается ядром.
    stage?: string | null;
    stage_note?: string | null;
  },
) {
  return patchJson<{ ok: boolean }>(`/channels/${channelId}`, fields);
}

/** Ф8-3 A7/П8: «Владелец смотрит эту беседу» — `clientId` вкладки, открытая беседа и тред (`null` — снять
 * присутствие этой вкладки). Только UI-сессия (cookie + CSRF). */
export function postPresence(clientId: string, channelId: number | null, threadExtId: string | null) {
  return postJson<{ ok: boolean }>('/presence', {
    client_id: clientId,
    channel_id: channelId,
    thread_ext_id: channelId === null ? null : threadExtId,
  });
}

/** Ф20-3 A1: «Владелец работает в хабе» (окно в фокусе и действие за 5 мин) — раз в 60 с; только UI-сессия. */
export function postPresenceActive() {
  return postJson<{ ok: boolean }>('/presence/active', {});
}

// --- Ф41-2б-2: постоянные сессии хаба (`routes/sessions.py`; действия — только UI-сессия). Отказы 409/410/422
// приходят русской строкой в `ApiError.message` — показываются как есть. ---

export function listHubSessions(opts: { projectId?: number } = {}) {
  return get<{ items: HubSession[]; today?: SessionsToday }>('/sessions', { project_id: opts.projectId });
}

export function getHubSession(sessionId: number) {
  return get<SessionDetail>(`/sessions/${sessionId}`);
}

/** `after` — новые после курсора; `before` — последние `limit` до курсора. По возрастанию `id`. */
export function listHubSessionEvents(sessionId: number, opts: { after?: number; before?: number; limit?: number } = {}) {
  return get<SessionEvent[]>(`/sessions/${sessionId}/events`, {
    after: opts.after,
    before: opts.before,
    limit: opts.limit,
  });
}

/** Ф41-3а: событие целиком (с `detail`) — подробности шага по клику. */
export function getHubSessionEvent(sessionId: number, eventId: number) {
  return get<SessionEventFull>(`/sessions/${sessionId}/events/${eventId}`);
}

/** Ф41-3б: поток набираемого текста сессий (кадры `live`), только кука UI. */
export function sessionsLiveUrl(): string {
  return API_BASE + '/sessions/live';
}

/** Ф60-1C: помощник на интервью (`/interviews*`, только кука UI). */
export function getInterviewCurrent() {
  return get<InterviewCurrent>('/interviews/current');
}

export function getInterviewLines(id: number, after: number) {
  return get<{ lines: InterviewLine[] }>(`/interviews/${id}/lines`, { after, limit: 500 });
}

export function startInterview(body: { session_id: number; mode: 'live' | 'button'; them_lang: 'ru-RU' | 'en-US' }) {
  return postJson<{ interview: Interview }>('/interviews', body);
}

export function interviewHint(id: number) {
  return postJson<InterviewHintResult>(`/interviews/${id}/hint`, {});
}

export function interviewPause(id: number) {
  return postJson<{ interview: Interview }>(`/interviews/${id}/pause`, {});
}

export function interviewResume(id: number) {
  return postJson<{ interview: Interview }>(`/interviews/${id}/resume`, {});
}

export function interviewEnd(id: number) {
  return postJson<InterviewHintResult>(`/interviews/${id}/end`, {});
}

/** Ф41-6б (раздел 5 п.3): линии «не в сети». Ручки может не быть (404) — вызывающий считает список пустым. */
export function listOfflineLines() {
  return get<{ lines: OfflineLine[] }>('/subs', { offline: 1 });
}

export function createHubSession(input: SessionCreateInput) {
  return postJson<{ session: HubSession; role_file_warning?: string; resume_warning?: string }>('/sessions', input);
}

/** Ф99-1: прошлые разговоры Claude Code папки проекта (только кука интерфейса), свежие сверху. */
export function getProjectConversations(projectId: number) {
  return get<{ items: ProjectConversation[]; workdir: string | null }>(`/projects/${projectId}/conversations`);
}

export function sayHubSession(sessionId: number, text: string, priority: 'next' | 'now', files?: string[]) {
  return postJson<{ id: number; session_id: number; priority: string; status: string }>(
    `/sessions/${sessionId}/say`,
    files !== undefined && files.length > 0 ? { text, priority, files } : { text, priority },
  );
}

/** Ф41-3в: файл во вход сессии — сырые байты (`Content-Length` ставит браузер по `Blob`). Тип ядро определяет по
 * сигнатуре. Отмена — `signal`; единственный повтор — после пустого 403 с устаревшим csrf, как у `uploadFile`. */
export async function uploadSessionFile(
  sessionId: number,
  file: Blob,
  name: string,
  signal: AbortSignal,
): Promise<SessionFileResult> {
  const used = csrf;
  const headers = (): Record<string, string> => ({
    'X-Hub-Client': CLIENT_NAME,
    'X-Hub-CSRF': csrf ?? '',
    'Content-Type': 'application/octet-stream',
  });
  const path = `/sessions/${sessionId}/files`;
  const query = { name };
  const attempt = () => sendRequest<SessionFileResult>(path, { query }, 'POST', headers(), file, signal);
  return attempt().catch(async (err: unknown) => {
    if (!(await csrfWentStale(err, 'POST', used))) throw err;
    return attempt();
  });
}

/** Ф41-3в: убрать неприложенный файл (приложенный — 409). */
export function deleteSessionFile(sessionId: number, fileId: string) {
  return del(`/sessions/${sessionId}/files/${encodeURIComponent(fileId)}`);
}

export type FallbackPatch = { engine: EngineName | null; model?: string | null; effort?: string | null } | null;

/** Ф41-4: заменить желаемую подписку сессии (`null` — подписка не нужна). */
export function patchHubSession(
  sessionId: number,
  body: {
    want_sub?: SessionWantSub | null;
    mode?: 'manual' | 'auto';
    model?: string | null;
    effort?: string | null;
    role?: string | null;
    number?: number | null;
    autosend_ok?: boolean;
    /** Ф48-2 D: порог сжатия (50 000…900 000, `null` — умолчание) и выключатель авто-сжатия. */
    compact_tokens?: number | null;
    compact_auto?: boolean;
    /** Ф83: запасной сессии — `null` как у проекта, `{engine: null}` нет, иначе свой. */
    fallback?: FallbackPatch;
    /** Ф48-3: файл роли (путь в проекте); `null` — снять. Действует со следующего запуска процесса. */
    role_file?: string | null;
  },
) {
  return patchJson<{ session: HubSession; role_file_warning?: string }>(`/sessions/${sessionId}`, body);
}

/** Ф48-3: окно «Инструкция роли» у сессии — общий текст хаба и файл роли. */
export function getSessionRoleText(sessionId: number) {
  return get<SessionRoleText>(`/sessions/${sessionId}/role-text`);
}

/** Ф72-1: MD-файл для окна просмотра. Относительный путь ядро ищет от `from`, папки сессии, папки проекта. */
export function getMdFile(path: string, ctx: { sessionId?: number; projectId?: number; from?: string } = {}) {
  return get<MdFile>('/files/md', {
    path,
    session_id: ctx.sessionId,
    project_id: ctx.projectId,
    from: ctx.from,
  });
}

/** Ф41-4: «Поднять» — поднять сессию и выдать ждущее (409 «поднимать не на что»). */
/** Ф83: «Вернуть на основной» (409 «<движок> на паузе до ЧЧ:ММ» — текст ядра в ошибке). */
export function returnHubSessionFallback(sessionId: number) {
  return postJson<{ session: HubSession; closed?: unknown }>(`/sessions/${sessionId}/fallback/return`, {});
}

export function wakeHubSession(sessionId: number) {
  return postJson<{ session: HubSession }>(`/sessions/${sessionId}/wake`, {});
}

/** Ф73-1: «Перечитать MCP» — перезапуск процесса сессии с продолжением разговора (после хода; спящую поднимет). */
export function mcpReloadHubSession(sessionId: number) {
  return postJson<{ session: HubSession }>(`/sessions/${sessionId}/mcp-reload`, {});
}

/** Ф41-4: «Сжать» контекст. */
export function compactHubSession(sessionId: number) {
  return postJson<{ session: HubSession }>(`/sessions/${sessionId}/compact`, {});
}

/** Ф41-4: сколько бесед «без подтверждения» у роли проекта (для формы новой сессии). */
export function getSessionAutosendCount(projectId: number, role: string) {
  return get<{ count: number }>('/sessions/autosend-count', { project_id: projectId, role });
}

/** Ф48-2 A: «Отправить снова» прерванную реплику. */
export function resendHubSessionSay(sessionId: number, sayId: number) {
  return postJson<{ id: number; status: string; session: HubSession }>(`/sessions/${sessionId}/says/${sayId}/resend`, {});
}

/** Ф48-2 A: «Убрать» прерванную реплику. */
export function dropHubSessionSay(sessionId: number, sayId: number) {
  return del(`/sessions/${sessionId}/says/${sayId}`);
}

export function interruptHubSession(sessionId: number) {
  return postJson<{ session: HubSession }>(`/sessions/${sessionId}/interrupt`, {});
}

export function sleepHubSession(sessionId: number) {
  return postJson<{ session: HubSession; changed: boolean; dropped_says: number }>(`/sessions/${sessionId}/sleep`, {});
}

export function closeHubSession(sessionId: number) {
  return postJson<{ session: HubSession; closed: boolean }>(`/sessions/${sessionId}/close`, {});
}

export function decideHubSessionRequest(
  requestId: number,
  body: { decision: 'allow' | 'deny'; answers?: Record<string, string>; message?: string; always?: boolean },
) {
  return postJson<{ id: number; session_id: number; status: string }>(`/session-requests/${requestId}/decide`, body);
}

/** Ф99-7.2: снять «Всегда в этой сессии» («⋯ → Разрешения…»); ответ — запись сессии. */
export function removeHubSessionAlways(sessionId: number, key: string) {
  return postJson<{ session: HubSession }>(`/sessions/${sessionId}/always/remove`, { key });
}

// --- Ф17-1b: заявки сессий на вступление в группы и каналы Telegram (`routes/joins.py`; решения — только UI) ---

export function listJoins(opts: { projectId?: number; status?: string; limit?: number } = {}) {
  return get<JoinRequest[]>('/joins', { project_id: opts.projectId, status: opts.status, limit: opts.limit });
}

export function approveJoin(joinId: number) {
  return postJson<JoinRequest>(`/joins/${joinId}/approve`, {});
}

export function declineJoin(joinId: number) {
  return postJson<JoinRequest>(`/joins/${joinId}/decline`, {});
}

// --- Ф38-2: заявки сессий на правило раскладки (`routes/rule_requests.py`; решения — только UI) ---

export function listRuleRequests(opts: { limit?: number } = {}) {
  return get<RuleRequest[]>('/rule-requests', { limit: opts.limit });
}

/** Ф41-5: панель «Автопилот». Ручек может не быть (404) — вызывающий молчит. */
export function getAutopilot() {
  return get<AutopilotState>('/autopilot');
}

export function putAutopilot(body: { scope: 'global' | number; key: string; hold: boolean | null }) {
  return putJson<AutopilotState>('/autopilot', body);
}

export function autopilotHoldAll() {
  return postJson<AutopilotState>('/autopilot/hold-all', {});
}

/** Ф44-1d: нажатие кнопки бота владельца (кука -> строка сразу `approved`); повтор — та же живая строка. */
export function pressButton(messageId: number, body: { row: number; col: number; text: string }) {
  return postJson<ButtonPressReply>(`/messages/${messageId}/buttons/press`, body);
}

export function listButtonPresses(opts: { status?: string; limit?: number } = {}) {
  return get<ButtonPress[]>('/button-presses', { status: opts.status, limit: opts.limit });
}

export function listHumanRequests(opts: { status?: string; limit?: number } = {}) {
  return get<HumanRequest[]>('/human-requests', { status: opts.status, limit: opts.limit });
}

export function decideHumanRequest(id: number, outcome: 'done' | 'failed' | 'later') {
  return postJson<HumanRequest>(`/human-requests/${id}/${outcome}`, {});
}

export function approveButtonPress(pressId: number) {
  return postJson<ButtonPress>(`/button-presses/${pressId}/approve`, {});
}

export function declineButtonPress(pressId: number) {
  return postJson<ButtonPress>(`/button-presses/${pressId}/decline`, {});
}

/** Ф41-5: заявки агентов. */
export function listAgentRequests(opts: { status?: string } = {}) {
  return get<{ requests: AgentRequest[] }>('/agent-requests', { status: opts.status });
}

export function approveAgentRequest(requestId: number, body: AgentRequestApproveBody = {}) {
  return postJson<{ request: AgentRequest; session?: HubSession }>(`/agent-requests/${requestId}/approve`, body);
}

export function declineAgentRequest(requestId: number, note?: string) {
  return postJson<{ request: AgentRequest }>(`/agent-requests/${requestId}/decline`, note ? { note } : {});
}

/** Ф41-8: каталог ролей. */
export function listRoles() {
  return get<RoleInfo[]>('/roles');
}

/** Инструкция роли (только чтение): общий свод + текст роли. */
export function getRoleText(key: string) {
  return get<RoleText>(`/roles/${encodeURIComponent(key)}`);
}

/** Ф41-7: «Новая с передачей дел» — прежняя закрывается, новая заводится. */
export function handoffHubSession(sessionId: number, body: HandoffInput) {
  return postJson<{ session: HubSession; closed: HubSession }>(`/sessions/${sessionId}/handoff`, body);
}

export function approveRuleRequest(requestId: number) {
  return postJson<RuleRequest>(`/rule-requests/${requestId}/approve`, {});
}

export function declineRuleRequest(requestId: number) {
  return postJson<RuleRequest>(`/rule-requests/${requestId}/decline`, {});
}

// --- Ф17-2b: выход из групп и каналов Telegram (`routes/leaves.py`; решения и кнопка шапки — только UI) ---

export function listLeaves(opts: { projectId?: number; channelId?: number; status?: string; limit?: number } = {}) {
  return get<LeaveRequest[]>('/leaves', {
    project_id: opts.projectId,
    channel_id: opts.channelId,
    status: opts.status,
    limit: opts.limit,
  });
}

export function approveLeave(leaveId: number) {
  return postJson<LeaveRequest>(`/leaves/${leaveId}/approve`, {});
}

export function declineLeave(leaveId: number) {
  return postJson<LeaveRequest>(`/leaves/${leaveId}/decline`, {});
}

/** Кнопка «Выйти из …» в шапке беседы: новая заявка сразу `approved`, ждущая просьба сессии — одобряется. */
export function leaveChannel(channelId: number) {
  return postJson<LeaveRequest>(`/channels/${channelId}/leave`, {});
}

// --- Ф62-6: группы WhatsApp (`routes/group_ops.py`; решения и заявки владельца — только UI) ---

export function listGroupOps(opts: { projectId?: number; channelId?: number; status?: string; limit?: number } = {}) {
  return get<GroupOp[]>('/group-ops', {
    project_id: opts.projectId,
    channel_id: opts.channelId,
    status: opts.status,
    limit: opts.limit,
  });
}

/** Состав группы WhatsApp (только кука). `refresh` — запросить свежий состав у моста (задание `members`). */
export function getGroupMembers(channelId: number, refresh = false) {
  return get<GroupMembers>(`/channels/${channelId}/group-members`, { refresh: refresh ? 1 : undefined });
}

export function getGroupOp(opId: number) {
  return get<GroupOp>(`/group-ops/${opId}`);
}

/** Заявка владельца из интерфейса: сразу `approved`, карточки нет. */
export function createGroupOp(input: GroupOpInput) {
  return postJson<GroupOp>('/group-ops/ui', {
    kind: input.kind,
    project_id: input.projectId ?? undefined,
    account_id: input.accountId ?? undefined,
    channel_id: input.channelId ?? undefined,
    title: input.title,
    participants: input.participants,
  });
}

export function approveGroupOp(opId: number) {
  return postJson<GroupOp>(`/group-ops/${opId}/approve`, {});
}

export function declineGroupOp(opId: number) {
  return postJson<GroupOp>(`/group-ops/${opId}/decline`, {});
}

// --- Ф14-1: раскладка и проекты (`routes/organize.py`; все ручки — только UI-сессия) --------------------------

export function listMapRules() {
  return get<MapRule[]>('/map-rules');
}

/** `POST /map-rules`; `dryRun` — предпросмотр без записи («Проверить»). 404 аккаунт/проект, 422 условие
 * (текст ядра), 409 «такое правило уже есть (#N)» — `ApiError.message` = `detail`. */
export function addMapRule(input: MapRuleInput, opts: { dryRun: true }): Promise<MapPreview>;
export function addMapRule(input: MapRuleInput, opts?: { dryRun?: false }): Promise<MapRuleAdded>;
export function addMapRule(input: MapRuleInput, opts: { dryRun?: boolean } = {}): Promise<MapPreview | MapRuleAdded> {
  return postJson<MapPreview | MapRuleAdded>('/map-rules', { ...input, dry_run: opts.dryRun === true });
}

/** `DELETE /map-rules/{id}`; `dryRun` — предпросмотр ухода бесед без записи. */
export function deleteMapRule(ruleId: number, opts: { dryRun: true }): Promise<MapPreview>;
export function deleteMapRule(ruleId: number, opts?: { dryRun?: false }): Promise<MapRuleDeleted>;
export function deleteMapRule(ruleId: number, opts: { dryRun?: boolean } = {}): Promise<MapPreview | MapRuleDeleted> {
  return rawRequest<MapPreview | MapRuleDeleted>(`/map-rules/${ruleId}`, {
    method: 'DELETE',
    query: { dry_run: opts.dryRun === true ? 'true' : undefined },
  });
}

export function getMapOptions(accountId: number) {
  return get<MapOptions>(`/accounts/${accountId}/map-options`);
}

/** Проект из UI — только название и цвет (`#rrggbb`); 422 имя/цвет, 409 имя занято. */
export function createProject(input: { name: string; color?: string | null }) {
  return postJson<{ project_id: number }>('/projects', { name: input.name, color: input.color ?? null });
}

/** Ф99-8: очерёдность проектов — все id проектов в нужном порядке (422 — список не совпал). */
export function putProjectOrder(ids: number[]) {
  return putJson<{ ok: boolean }>('/projects/order', { ids });
}

/** `color: null` — снять цвет; отсутствующее поле не меняется. */
export function patchProject(
  projectId: number,
  fields: {
    name?: string;
    color?: string | null;
    session_join?: 'confirm' | 'auto';
    session_handled?: boolean;
    /** Ф64-1: проект «по времени». */
    workday?: boolean;
    /** Ф86-1: сводка дня после сжатия и слова владельца из соседних сессий. */
    digest_after_compact?: boolean;
    sibling_says?: boolean;
    /** Ф48-4: сообщения агентов пачкой, минуты 0–60 (0 — сразу). */
    agent_batch_min?: number;
    /** Ф52-2: аккаунты, чьё «Неразобранное» слышит проект; `[]` — снять. */
    unsorted_accounts?: number[];
    /** Ф66-1: кто отвечает на карточки — только названные группы (частичное слияние). */
    card_route?: Record<string, CardRoute>;
    /** Ф83: запасной движок сессий проекта; `null` — снять, `{engine: null}` — «нет». */
    session_fallback?: { engine: EngineName | null; model?: string | null; effort?: string | null } | null;
  },
) {
  return patchJson<{ ok: boolean }>(`/projects/${projectId}`, fields);
}

/** Ф64-1: «Закончить день» (`open: false`) / «Начать день»; 409 `workday_off` — проект не «по времени». */
export function setProjectDay(projectId: number, open: boolean) {
  return postJson<{ open: boolean; ts: number | null }>(`/projects/${projectId}/day`, { open });
}

/** Перенос пачкой (1..200 без повторов), одна транзакция: неизвестный id -> 404, ничего не записано. */
export function moveChannels(channelIds: number[], target: MoveTarget) {
  return postJson<{ moved: number }>('/channels/move', { channel_ids: channelIds, project_id: target });
}

// --- Ф21-1b: люди (`routes/people.py`; правка и предложения — только UI-сессия) ----------------------------------

/** Клик по отправителю: `contact_id` = `message.sender.id`. 404 `detail`: `is_me` | `not_person` | `not_found`
 * (Р1: код — в `ApiError.message`). */
export function getPersonOfContact(contactId: number) {
  return get<PersonCard>(`/people/of-contact/${contactId}`);
}

export function getPerson(personId: number) {
  return get<PersonCard>(`/people/${personId}`);
}

export function listPeople(opts: { q?: string; scope?: 'known' | 'all'; limit?: number; offset?: number } = {}) {
  return get<PeopleList>('/people', {
    q: opts.q === undefined || opts.q === '' ? undefined : opts.q,
    scope: opts.scope ?? 'known',
    limit: opts.limit ?? 50,
    offset: opts.offset ?? 0,
  });
}

/** Ф77-1: реестр машин. */
export function listMachines() {
  return get<MachineList>('/machines');
}

/** Ф77-1: имя машины (только кука интерфейса); пустая строка снимает имя. 404 — нет машины, 422 — имя не годится. */
export function renameMachine(host: string, name: string) {
  return patchJson<{ machine: Machine }>(`/machines/${encodeURIComponent(host)}`, { name });
}

// --- Ф82-1: «Мои данные» агента владельца (только кука интерфейса; значения — по одному, по клику) ---------------

export function getMyData() {
  return get<MyDataOverview>('/my-data');
}

export function getMyDataFields(scope: string) {
  return get<MyDataFields>(`/my-data/${encodeURIComponent(scope)}`);
}

export function getMyDataValue(scope: string, key: string) {
  return get<{ key: string; value: string; from_common: boolean }>(
    `/my-data/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`,
  );
}

/** Запись значения; у проекта пустая строка — «пусто в этом проекте» (перекрывает общее). */
export function putMyDataValue(scope: string, key: string, value: string) {
  return putJson<MyDataField>(`/my-data/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`, { value });
}

export function deleteMyDataValue(scope: string, key: string) {
  return del(`/my-data/${encodeURIComponent(scope)}/${encodeURIComponent(key)}`);
}

export function getMyDataJournal(scope: string, limit = 50) {
  return get<{ scope: string; items: string[] }>('/my-data/journal', { scope, limit });
}

export function getPeopleSuggestions() {
  return get<PersonSuggestion[]>('/people/suggestions');
}

/** Объединить `from` в `into`; ответ — карточка `into`. 422 — нарушение «разные» (текст ядра). */
export function mergePeople(into: number, from: number[]) {
  return postJson<PersonCard>('/people/merge', { into, from });
}

/** Отделить контакты в нового человека; ответ — карточка нового. */
export function splitPerson(contactIds: number[]) {
  return postJson<PersonCard>('/people/split', { contact_ids: contactIds });
}

/** `name: null` — вернуть автоматическое имя. */
export function renamePerson(personId: number, name: string | null) {
  return patchJson<PersonCard>(`/people/${personId}`, { name });
}

/** Ф24-1c: ручные ручки фактов (только UI-сессия); ответ — карточка человека, 422 — текст ядра. */
export function addPersonFact(personId: number, text: string) {
  return postJson<PersonCard>(`/people/${personId}/facts`, { text });
}

export function editPersonFact(personId: number, factId: number, text: string) {
  return patchJson<PersonCard>(`/people/${personId}/facts/${factId}`, { text });
}

export function removePersonFact(personId: number, factId: number) {
  return rawRequest<PersonCard>(`/people/${personId}/facts/${factId}`, { method: 'DELETE' });
}

export function notSamePeople(a: number, b: number) {
  return postJson<null>('/people/not-same', { a, b });
}

// --- Ф34-2: напоминания по беседам (`routes/reminders.py`; владелец ставит и правит в UI) ---

export function listReminders(
  opts: { channelId?: number; projectId?: number; all?: boolean; counts?: boolean; limit?: number } = {},
) {
  return get<{ items: Reminder[] }>('/reminders', {
    channel_id: opts.channelId,
    project_id: opts.projectId,
    all: opts.all ? 1 : undefined,
    counts: opts.counts ? 1 : undefined,
    limit: opts.limit,
  });
}

/** Ф89-1: без беседы — `project_id` обязателен, с беседой его нельзя; `every` — `day|weekdays|week|<N>h`. */
export interface ReminderInput {
  at: string;
  channel_id?: number;
  project_id?: number;
  note?: string;
  every?: string;
  cancel_if_reply?: boolean;
}

export interface ReminderPatch {
  at?: string;
  note?: string;
  every?: string | null;
  cancel_if_reply?: boolean;
  close?: boolean;
  stop?: boolean;
}

/** `at` — `"YYYY-MM-DD HH:MM"` в поясе аккаунта беседы или ISO со смещением. */
export function createReminder(input: ReminderInput) {
  return postJson<Reminder>('/reminders', input);
}

export function patchReminder(
  reminderId: number,
  fields: ReminderPatch,
) {
  return patchJson<Reminder>(`/reminders/${reminderId}`, fields);
}

// --- Ф35-3: сообщения агентов и диалог владелец <-> агент (`routes/peer_msgs.py`) ---

export function listPeerMsgs(before?: number, limit = 200) {
  return get<{ items: PeerMsg[] }>('/peer-msgs', { limit, before });
}

export type PeerSendInput =
  | { text: string; to: { project_id: number; role?: string; number?: number; sub_id?: number } }
  | { text: string; reply_to: number };

export function sendPeerMsg(input: PeerSendInput) {
  return postJson<PeerSendResult>('/peer-msgs', input);
}

export function markPeerMsgsRead(ids: number[]) {
  return postJson<{ marked: number }>('/peer-msgs/read', { ids });
}

// Ф56-3: очередь владельца — окно «Агент».
export function getOwnerQueue() {
  return get<OwnerQueue>('/owner_channel/queue');
}

// Ф58-1: разговор владельца с его агентом.
export function getOwnerTalk(params: { before?: number; limit?: number; session_id?: number | null } = {}) {
  const q = new URLSearchParams();
  if (params.session_id !== undefined) q.set('session_id', params.session_id === null ? 'general' : String(params.session_id));
  if (params.before !== undefined) q.set('before', String(params.before));
  if (params.limit !== undefined) q.set('limit', String(params.limit));
  const qs = q.toString();
  return get<{ items: OwnerTalkMsg[]; has_more: boolean }>(`/owner_channel/talk${qs === '' ? '' : `?${qs}`}`);
}

// Ф66-2: ветки разговора («Общее» и по сессиям).
export function getOwnerTalkBranches() {
  return get<{ items: OwnerTalkBranch[] }>('/owner_channel/talk/branches');
}

export function postOwnerTalk(body: {
  text: string;
  ref?: string | null;
  reply_to?: number | null;
  session_id?: number | null;
  files?: string[];
}) {
  return postJson<OwnerTalkMsg>('/owner_channel/talk', body);
}

/** 23.10: файл к реплике разговора с агентом — сырые байты, как `uploadSessionFile`. */
export async function uploadTalkFile(file: Blob, name: string, signal: AbortSignal): Promise<SessionFileResult> {
  const used = csrf;
  const headers = (): Record<string, string> => ({
    'X-Hub-Client': CLIENT_NAME,
    'X-Hub-CSRF': csrf ?? '',
    'Content-Type': 'application/octet-stream',
  });
  const attempt = () =>
    sendRequest<SessionFileResult>('/owner_channel/talk/files', { query: { name } }, 'POST', headers(), file, signal);
  return attempt().catch(async (err: unknown) => {
    if (!(await csrfWentStale(err, 'POST', used))) throw err;
    return attempt();
  });
}

/** 23.10: убрать неприложенный файл разговора (приложенный — 409). */
export function deleteTalkFile(fileId: string) {
  return del(`/owner_channel/talk/files/${encodeURIComponent(fileId)}`);
}

/** 23.10: адрес байтов приложенного файла разговора (картинка — показать, прочее — скачать). */
export function talkFileUrl(fileId: string): string {
  return `${API_BASE}/owner_channel/talk/files/${encodeURIComponent(fileId)}`;
}

export function markOwnerTalkRead(upto: number, sessionId?: number | null) {
  return postJson<unknown>('/owner_channel/talk/read', sessionId === undefined ? { upto } : { upto, session_id: sessionId });
}

// Ф75-1: модель агента владельца — читать (кука), выбрать или снять выбор (`model: null`; только кука).
export function getOwnerAgentSettings() {
  return get<OwnerAgentSettings>('/owner_channel/agent-settings');
}

export function putOwnerAgentSettings(body: { model: string | null; variant: string | null }) {
  return putJson<OwnerAgentSettings>('/owner_channel/agent-settings', body);
}

export function getOwnerProjectModel(projectId: number) {
  return get<OwnerProjectModel>(`/owner_channel/agent-settings/projects/${projectId}`);
}

export function putOwnerProjectModel(projectId: number, body: { model: string | null; variant: string | null }) {
  return putJson<OwnerProjectModel>(`/owner_channel/agent-settings/projects/${projectId}`, body);
}

// Ф76-4б: образцы решений владельца, правила, журнал решений агента (кука интерфейса).
export function getPatterns(projectId: number) {
  return get<PatternsState>(`/patterns/${projectId}`);
}

export function putPatterns(projectId: number, body: PatternsPut) {
  return putJson<unknown>(`/patterns/${projectId}`, body);
}

export function getAgentDecisions(projectId: number, limit = 30) {
  return get<{ items: AgentDecision[] }>(`/patterns/${projectId}/agent-decisions?limit=${limit}`);
}

export function markDecisionWrong(id: number, note?: string) {
  return postJson<{ id: number; wrong_ts: number | null; dropped: string | null }>(
    `/patterns/decisions/${id}/wrong`,
    note === undefined ? {} : { note },
  );
}

export function answerOwner(ref: string, body: OwnerAnswerBody) {
  return postJson<{ ref: string; status: string }>(`/owner_channel/${encodeURIComponent(ref)}/answer`, body);
}

export function createErrand(ref: string, body: { action?: string; text?: string }) {
  return postJson<{ errand: OwnerErrand }>(`/owner_channel/${encodeURIComponent(ref)}/errand`, body);
}

/** Ф65-1: закрытые строки очереди с итогом агента (`errand_result`). */
export function getOwnerClosed(limit = 30) {
  return get<{ items: OwnerClosedRow[] }>(`/owner_channel/closed?limit=${limit}`);
}

export function cancelErrand(id: number) {
  return del(`/owner_channel/errands/${id}`);
}

/** Ф96-1: руки у владельца / у агента посреди поручения (кука интерфейса). 409 — поручение закрыто. */
export function errandHands(id: number, to: 'owner_channel' | 'agent', note?: string) {
  const body: { to: string; note?: string } = { to };
  if (to === 'agent' && note !== undefined && note.trim() !== '') body.note = note.trim();
  return postJson<{ errand: OwnerErrand; changed: boolean }>(`/owner_channel/errands/${id}/hands`, body);
}

// --- Ф56-5б: терминал (только кука интерфейса; контракт — `docs/architecture.md`) -------------

export function listTerms() {
  return get<TermInfo[]>('/term');
}

export function createTerm(size?: { cols: number; rows: number }) {
  return postJson<TermInfo>('/term', size ?? {});
}

export function closeTerm(id: number) {
  return del(`/term/${id}`);
}

export function termInput(id: number, dataB64: string) {
  return postJson<{ ok: boolean }>(`/term/${id}/input`, { data_b64: dataB64 });
}

export function termResize(id: number, cols: number, rows: number) {
  return postJson<{ cols: number; rows: number }>(`/term/${id}/resize`, { cols, rows });
}

export function termRun(id: number, text: string, source: TermRunSource, force = false) {
  return postJson<{ run_id: number; term_id: number }>(
    `/term/${id}/run`,
    force ? { text, source, force: true } : { text, source },
  );
}

export function listTermRuns(limit = 50) {
  return get<TermRunRecord[]>('/term/runs', { limit });
}

/** Один поток на вкладку, все терминалы. `vector` — `<id>@<эпоха>:<n>` через запятую (пусто — всё снимками). */
export function termStreamUrl(vector = ''): string {
  return API_BASE + '/term/stream' + (vector === '' ? '' : `?pos=${encodeURIComponent(vector)}`);
}

/** Отписать внешнюю сессию от хаба (интерфейс, кука): `POST /subs/{id}/end` с пустым телом; 404 — подписки уже нет. */
export function endSub(subId: number) {
  return postJson<{ sub: ProjectSub }>(`/subs/${subId}/end`, {});
}

// --- Ф63-1: правила входящих проекта (`/projects/{id}/inbound-rules`) ----------------------------------------

export function listInboundRules(projectId: number) {
  return get<InboundRulesList>(`/projects/${projectId}/inbound-rules`);
}

export function addInboundRule(projectId: number, input: InboundRuleInput) {
  return postJson<InboundRule>(`/projects/${projectId}/inbound-rules`, input);
}

export function patchInboundRule(projectId: number, ruleId: number, patch: Partial<InboundRuleInput>) {
  return patchJson<InboundRule>(`/projects/${projectId}/inbound-rules/${ruleId}`, patch);
}

export function deleteInboundRule(projectId: number, ruleId: number) {
  return del(`/projects/${projectId}/inbound-rules/${ruleId}`);
}
