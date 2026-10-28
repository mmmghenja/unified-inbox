// Единственный модуль-стор (спека: «без pinia — один модуль-стор на reactive»). Состояние экрана (какой
// проект/канал открыт) хранится здесь же — роутера в проекте нет (спека, «Сборка и раздача»).
import { reactive } from 'vue';

import * as api from '../api/client';
import type {
  AgentRequest,
  AgentRequestApproveBody,
  AgentRun,
  HandoffInput,
  ApproveBatchResult,
  AttentionMode,
  Channel,
  ButtonPress,
  HumanRequest,
  OwnerAnswerBody,
  OwnerItem,
  JoinRequest,
  RuleRequest,
  PeerMsg,
  ProjectSub,
  PeerSendResult,
  Reminder,
  GroupOp,
  GroupOpInput,
  LeaveRequest,
  HomeReactItem,
  InboxItem,
  Message,
  MessageEdit,
  MoveTarget,
  OutboxListItem,
  OutboxStatus,
  Project,
  ResolveUnknownDecision,
  SearchResult,
  StatusResponse,
  SyncResponse,
  ThreadListItem,
  UiSession,
  UploadResult,
  HubSession,
  SessionCreateInput,
  SessionEvent,
  SessionFileDraft,
  SessionWantSub,
  SessionRequest,
} from '../api/types';
import { ALL_PROJECTS_ID, UNSORTED_PROJECT_ID } from '../api/types';
import {
  applyMediaUpdate,
  applyMessageOp,
  applySync,
  createSyncState,
  mergeMessagePage,
  removeChannel,
  removeMessage,
  type SyncState,
} from './applySync';
import {
  applySessionRequest,
  createSessionsState,
  applyLiveFrame,
  detailKey,
  firstSessionEventId,
  lastSessionEventId,
  mergeSessionEvents,
  replaceSessions,
  setSessionPending,
  setSessionSays,
  upsertSession,
  type SessionsState,
} from './sessions';
import { boardState, openColumn, replaceColumn, sameItem, type ColumnItem } from './board';
import { applyHumanRequest, createHumanState, type HumanState } from './humanRequests';
import { applyOwnerQueue, createOwnerState, type OwnerState } from './owner_channel';
import { humanErrandsMark } from '../util/owner_channel';
import { applyPress, createPressState, pressKey, type PressState } from './presses';
import { applyJoin, createJoinState, type JoinState } from './joins';
import { applyAgentRequest, createAgentRequestState, type AgentRequestState } from './agentRequests';
import { applyRuleRequest, createRuleRequestState, type RuleRequestState } from './ruleRequests';
import {
  applyPeerMsg,
  createPeerMsgState,
  unreadReplies,
  type AgentAddress,
  type PeerMsgState,
} from './peerMsgs';
import { applyReminder, createReminderState, type ReminderState } from './reminders';
import { applyGroupOp, createGroupOpState, type GroupOpState } from './groupOps';
import { applyLeave, createLeaveState, type LeaveState } from './leaves';
import { createStaleness, startStalenessClock, type StalenessState } from './staleness';
import { ActivityTracker } from './activity';
import { defaultIsActive, PresenceTracker } from './presence';
import { connectStream, type EventSourceLike, type StreamHandle } from './stream';
import { mergeThreadMessagePage, threadKey, type OpenThreadRef } from './threads';
import { startOfDay } from '../util/home';
import { isPhone } from '../util/layout';
import { applyInterviewFrame, interviewUi, loadInterviewCurrent } from './interview';
import { patchFieldsOf } from '../util/mapping';
import { passwordLoginError, RENEW_INTERVAL_MS } from '../util/remote';
import { channelsOfProject } from './selectors';
export type Screen = 'loading' | 'login' | 'app' | 'session-expired';

export interface StoreState {
  screen: Screen;
  sync: SyncState;
  projects: Project[];
  inbox: InboxItem[];
  currentProjectId: number | null;
  currentChannelId: number | null;
  // Спека 2e, раздел «Панель треда»: открыт один тред за раз; смена канала закрывает панель (openChannel).
  openThread: OpenThreadRef | null;
  status: StatusResponse | null;
  staleness: StalenessState;
  /** По каналу, не общий (F6 приёмки 6b): открытие двух каналов подряд не путает спиннер одного с другим. */
  loadingMessagesFor: Record<number, boolean>;
  nextBefore: Record<number, string | null>; // курсор «догрузить выше» на канал
  // Ф8-3 D4: браузерных уведомлений UI больше нет (кнопки «Включить уведомления», тихих часов и `maybeNotify`)
  // — уведомления даёт разборщик: он работает и без открытого UI и знает присутствие.
  error: string | null;
  /** Ф8-1 A4: последняя неудача цикла `/sync` (сеть, 5xx, таймаут); `null` — последний `/sync` прошёл.
   * Текст ошибки сервера в интерфейс не выводится (он в консоли) — `TopBar.vue` показывает только давность. */
  syncError: SyncErrorInfo | null;
  /** Ф8-6 C1: открыта главная (первый пункт левой колонки; после входа — по умолчанию). */
  homeOpen: boolean;
  /** Ф20-4 B1: открыта вкладка «Claude» (оркестратор и сессии по проектам); по образцу `homeOpen`. */
  claudeOpen: boolean;
  /** Раздел главной, к которому прокрутить после открытия (`drafts` — кнопка «ждёт: N»). */
  homeAnchor: HomeAnchor | null;
  home: HomeData;
  /** Ф8-6 C1: прокрутить открытую беседу к сообщению и подсветить его (клик на главной). `outboxId` —
   * строка черновика: сообщение ищется по `outbox_id`, когда страница беседы загрузится. */
  focus: FocusTarget | null;
  /** Ф41-2б-2: постоянные сессии хаба — записи, запросы, очередь реплик, события. */
  sessions: SessionsState;
  /** Ф17-1b: заявки сессий на вступление в группы и каналы Telegram. */
  joins: JoinState;
  /** Ф38-2: заявки сессий на правило раскладки. */
  ruleRequests: RuleRequestState;
  /** Ф41-5: заявки агентов (перенос беседы, напарник). */
  agentRequests: AgentRequestState;
  /** Ф44-1d: нажатия кнопок ботов (карточки «Нажать?», исходы под клавиатурой). */
  presses: PressState;
  humans: HumanState;
  /** Ф56-3: очередь владельца — окно «Агент». */
  owner_channel: OwnerState;
  /** Ф41-7: id закрытой «с передачей дел» сессии -> id новой (для строки «дела переданы сессии #N»). */
  handoffTo: Record<number, number>;
  /** Ф34-2: напоминания по беседам (ждущие, сработавшие и недавно закрытые). */
  reminders: ReminderState;
  /** Ф35-3: сообщения агентов, диалог владельца с агентами и агенты в сети (вкладка «Claude»). */
  peerMsgs: PeerMsgState;
  /** Ф17-2b: заявки на выход из групп и каналов Telegram. */
  leaves: LeaveState;
  /** Ф62-6: заявки на группы WhatsApp. */
  groupOps: GroupOpState;
  /** Ф10-6 B4: нижняя вкладка телефона (в режимах `full`/`half` не используется). После входа — «Ждёт». */
  phoneTab: PhoneTab;
  /** Ф10-6 B3 (решение 4): последняя открытая беседа по вкладке проекта (id проекта или псевдо-проекта). */
  lastChannelByProject: Record<number, number>;
  /** Ф10-6 (при слиянии): показать столбец проекта на доске и задачу в нём («к дорожке», «ход окончен»). */
  boardFocus: BoardFocus | null;
  /** Ф10-7: эта сессия — удалённая (телефон через туннель; `/auth/me.remote`). Панель доступа тогда только
   * показывает состояние, без правки (правка — только с ноутбука). */
  remoteSession: boolean;
  /** Ф12-2 C: выбранные флажками черновики — в порядке выбора (в нём же раздаются слоты графика). */
  draftSelection: number[];
  /** Ф12-5 C2: поле ввода этой беседы берёт фокус, как только смонтируется (новая беседа из «Новое
   * сообщение»); поле само сбрасывает флаг. */
  composerFocusChannelId: number | null;
  /** Ф41-6б: беседа активного столбца «Главной» (указатель над ней / фокус внутри). */
  boardActiveChannelId: number | null;
  /** Беседа, открытая окном (модалкой) поверх «Главной» компьютера; `null` — окна нет. */
  channelModalId: number | null;
  /** Ф46-1: окно открыто «поверх любого экрана» (Написать автору): уход с «Главной» его не закрывает. */
  channelModalOverlay: boolean;
  /** Ф41-6б: что открыто на весь экран во вкладке «Сессии» телефона (уровень истории `session`). */
  phoneSession: ColumnItem | null;
}

export interface BoardFocus {
  projectId: number;
  /** Ф41-2б-2: раскрыть эту постоянную сессию в секции проекта. */
  sessionId?: number | null;
  /** Счётчик: повторный переход к тому же столбцу снова прокручивает и подсвечивает. */
  seq: number;
}

/** Ф10-6 B4: `wait` — «Ждёт», `projects` — доска, `chats` — беседы, `orch` — оркестратор. */
// Ф41-6б: `orch` — вкладка «Сессии» (ключ прежний), `dispatch` — страница «Агенты» (из меню и со вкладки «Сессии»).
export type PhoneTab = 'wait' | 'projects' | 'chats' | 'orch' | 'agent' | 'dispatch';

/** Ф10-6 B4 (решение 8): метка своих записей истории телефона — «Назад» закрывает верхний уровень. */
export type PhoneLevel = 'channel' | 'thread' | 'session';
/** Ф23-1: страница ленты беседы и ответов треда — при открытии и при догрузке вверх. */
export const MESSAGE_PAGE = 30;
const PHONE_HISTORY_KEY = 'hubPhone';

export type HomeAnchor = 'react' | 'drafts' | 'claude' | 'sent';

export interface HomeData {
  react: HomeReactItem[];
  /** Черновики (`pending_approval`) — и для главной, и для закреплённых над полем ввода (C2). */
  drafts: OutboxListItem[];
  /** Ф12-2 C: подтверждённые черновики по графику, ждущие своего слота (`queued`, `paced`). */
  scheduled?: OutboxListItem[];
  runs: AgentRun[];
  sent: OutboxListItem[];
  /** Ф37-3 C3: «Разобрано сессией» — строки по всем проектам, новые первыми. */
  loadedAt: number | null;
  error: string | null;
}

export interface FocusTarget {
  channelId: number;
  messageId: number | null;
  outboxId: number | null;
  /** Ф66-5: сообщение внутри треда — `ext_id` родителя; беседа откроется сразу с панелью этого треда. */
  threadExtId?: string | null;
  /** Счётчик: повторный клик по той же цели снова прокручивает и подсвечивает. */
  seq: number;
}

export interface SyncErrorInfo {
  at: number;
  message: string;
}

function createState(): StoreState {
  return reactive({
    screen: 'loading',
    sync: createSyncState(),
    projects: [],
    inbox: [],
    currentProjectId: null,
    currentChannelId: null,
    openThread: null,
    status: null,
    staleness: createStaleness(),
    loadingMessagesFor: {},
    nextBefore: {},
    error: null,
    syncError: null,
    homeOpen: true,
    claudeOpen: false,
    homeAnchor: null,
    home: { react: [], drafts: [], scheduled: [], runs: [], sent: [], loadedAt: null, error: null },
    focus: null,
    sessions: createSessionsState(),
    joins: createJoinState(),
    ruleRequests: createRuleRequestState(),
    agentRequests: createAgentRequestState(),
    presses: createPressState(),
    humans: createHumanState(),
    owner_channel: createOwnerState(),
    handoffTo: {},
    reminders: createReminderState(),
    peerMsgs: createPeerMsgState(),
    leaves: createLeaveState(),
    groupOps: createGroupOpState(),
    phoneTab: 'wait',
    lastChannelByProject: {},
    boardFocus: null,
    remoteSession: false,
    draftSelection: [],
    composerFocusChannelId: null,
    boardActiveChannelId: null,
    channelModalId: null,
    channelModalOverlay: false,
    phoneSession: null,
  }) as StoreState;
}

/** Ф41-3б: минимум от `EventSource` для потока `live` (кадры несут данные — слушатель получает событие). */
export interface LiveSourceLike {
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

/** Инъекции для тестов — по умолчанию пусто (настоящий браузерный `EventSource`). */
export interface StoreDeps {
  createEventSource?: (url: string) => EventSourceLike;
  /** Ф41-3б: поток набираемого текста сессий — по умолчанию браузерный `EventSource`. */
  createLiveEventSource?: (url: string) => LiveSourceLike;
  /** Ф8-3 D3: «окно видимо и в фокусе» для присутствия (тесты подменяют; по умолчанию — `document`). */
  presenceActive?: () => boolean;
}

const RECONNECT_DELAYS_MS = [1000, 2000, 5000, 10_000]; // F2: бэкофф 1→2→5→10с, дальше остаётся на потолке
const PROJECTS_REFRESH_MIN_INTERVAL_MS = 1000; // F4: троттлинг перезапроса счётчиков проектов
// Ф8-1 A4: повтор неудавшегося цикла `/sync` — 2 -> 5 -> 10 -> 30 с, дальше остаётся на потолке.
export const SYNC_RETRY_DELAYS_MS = [2000, 5000, 10_000, 30_000];
// Ф8-1 A5: страховочный `/sync` независимо от SSE — раз в 30 с при видимой вкладке, раз в 2 мин при скрытой.
export const SAFETY_SYNC_VISIBLE_MS = 30_000;
export const SAFETY_SYNC_HIDDEN_MS = 120_000;
// Ф8-6 C1: главная перезапрашивается при изменениях `/sync` не чаще раза в 2 с (и раз в 30 с — `HomeView`).
export const HOME_REFRESH_MIN_INTERVAL_MS = 2000;
export const HOME_POLL_MS = 30_000;
/** Ф56-3: окно «Агент» — перечитка очереди по `/sync` не чаще, и таймер сроков. */
const OWNER_REFRESH_MIN_MS = 2_000;
const OWNER_POLL_MS = 60_000;
/** Правки Ф65-1 15.10: пока открыто поручение агента — очередь раз в 5 с (агент пишет ход раз в 10 с). */
export const OWNER_ERRAND_POLL_MS = 5_000;
export const AGENT_POLL_MS = 30_000;
export const AGENT_REFRESH_MIN_MS = 5_000;
// Ф41-2б-2: список сессий опрашивается раз в 30 с ради `cache` и `listening` (в `/sync` их нет).
export const SESSION_POLL_MS = 30_000;
// Ф41-3б: пауза перед переподключением потока набираемого текста и срок жизни строки «пишет…» после `done`.
/** Ф41-3в: файлов в одной реплике (предел ядра). */
export const SESSION_FILES_MAX = 10;
export const LIVE_RECONNECT_MS = 2000;
export const LIVE_DONE_LINGER_MS = 5000;
// Ф9-2 D: события видимых сессий дочитываются не чаще раза в 1 с.
export const WORK_EVENTS_MIN_INTERVAL_MS = 1000;
/** B: сколько событий показывать сразу и догружать «показать раньше». */
export const WORK_EVENTS_PAGE = 30;
const WORK_EVENTS_AFTER_LIMIT = 200;
/** Итог действия владельца: `stale` — 409 (суть обновилась / запрос протух), `error` — прочее. */
export type WorkActionResult = 'ok' | 'stale' | 'error';

/** Итог действия владельца над сессией хаба: отказ несёт готовую русскую строку ядра (409/410/422) как есть. */
export type SessionActionResult =
  | {
      ok: true;
      /** Ф48-3: принято, но есть предупреждение ядра (файл роли не прочитать) — готовая строка. */
      warning?: string;
      /** Ф99-1: прошлый разговор менялся только что — подсказка ядра «закрой другое окно». */
      resumeWarning?: string;
    }
  | { ok: false; stale: boolean; message: string };

function asList<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/** Ф12-2 C: из очереди (`queued`) — только черновики по графику и (Ф30-1 П3) к точному времени, ближайшие
 * первыми. */
function scheduledOf(value: unknown): OutboxListItem[] {
  return asList<OutboxListItem>(value)
    .filter((item) => item.paced === true || typeof item.send_at_ts === 'number')
    .sort((a, b) => (a.not_before_ts ?? 0) - (b.not_before_ts ?? 0) || a.id - b.id);
}

/** Ф48-2 A: 404 ручек реплики («нет такой реплики») — не сбой сети, а устаревшее окно: показывается текстом. */
async function notFoundAsStale<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (err) {
    if (err instanceof api.ApiError && err.status === 404) throw new api.ApiError(409, 'Реплики уже нет — окно обновилось.', err.body);
    throw err;
  }
}

export class Store {
  readonly state: StoreState;
  private readonly deps: StoreDeps;
  private stopClock: (() => void) | null = null;
  private stream: StreamHandle | null = null;
  private syncing = false;
  private syncAgainAfter = false;
  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastProjectsRefreshAt = 0;
  private projectsRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  private loadingOlder = new Set<number>();
  private channelOpenTicket: Record<number, number> = {};
  private threadOpenTicket: Record<string, number> = {};
  private loadingOlderReplies = new Set<string>();
  private syncRetryAttempt = 0;
  private syncRetryTimer: ReturnType<typeof setTimeout> | null = null;
  private safetySyncTimer: ReturnType<typeof setTimeout> | null = null;
  private presence: PresenceTracker | null = null;
  private activity: ActivityTracker | null = null;
  private lastHomeRefreshAt = 0;
  private homeRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  private focusSeq = 0;
  private boardFocusSeq = 0;
  private lastSessionEventsAt = 0;
  private sessionEventsTimer: ReturnType<typeof setTimeout> | null = null;
  private sessionEventsDirty = new Set<number>();
  private sessionEventsLoading = new Set<number>();
  private sessionWatchers = 0;
  private sessionPollTimer: ReturnType<typeof setInterval> | null = null;
  private sessionFileSeq = 0;
  private sessionFileUploads = new Map<number, AbortController>();
  private liveWatchers = 0;
  private liveSource: LiveSourceLike | null = null;
  private liveReconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private liveDoneTimers = new Map<number, ReturnType<typeof setTimeout>>();
  private liveVisibilityListener: (() => void) | null = null;
  private renewTimer: ReturnType<typeof setTimeout> | null = null;
  private lastRenewAt = 0;

  constructor(deps: StoreDeps = {}) {
    this.state = createState();
    this.deps = deps;
  }

  // --- вход -----------------------------------------------------------------------------------------------

  /** Вызывать при старте приложения (`main.ts`): читает код из `location.hash` (если есть — сразу
   * `history.replaceState`, ДО любого запроса), иначе пробует восстановить сессию через `GET /auth/me`. */
  async bootstrap(win: LoginWindowLike = window): Promise<void> {
    api.setUnauthorizedHandler(() => {
      this.stopBackground();
      this.state.screen = 'session-expired';
    });

    const fromHash = await loginFromLocationHash(win, api.login);
    if (fromHash.attempted) {
      if (fromHash.ok) {
        await this.afterLogin();
      } else {
        this.state.screen = 'login';
      }
      return;
    }

    try {
      // silent401: 401 здесь значит «сессии ещё не было» (обычный исход при первом визите/после logout),
      // а не «истекла» — этот try/catch уже решает, что показать; глобальный обработчик тут ни при чём (F10).
      const me = await api.authMe({ silent401: true });
      this.state.remoteSession = me.remote === true;
      await this.afterLogin();
    } catch {
      this.state.screen = 'login';
    }
  }

  async loginWithCode(code: string): Promise<boolean> {
    try {
      await api.login(code);
    } catch {
      return false;
    }
    this.state.remoteSession = false;
    await this.afterLogin();
    return true;
  }

  /** Ф10-7: вход паролем с телефона. `null` — вошли; иначе текст ошибки для экрана входа. */
  async loginWithPassword(password: string): Promise<string | null> {
    try {
      await api.loginWithPassword(password);
    } catch (err) {
      return passwordLoginError(err);
    }
    this.state.remoteSession = true;
    await this.afterLogin();
    return null;
  }

  // --- Ф10-7: продление сессии -------------------------------------------------------------------------------

  /** `POST /auth/renew` сейчас и следующий — через 6 ч. Ошибка не страшна: 401 сам переведёт экран через
   * общий обработчик, сеть — следующая попытка по таймеру или при возврате на вкладку. */
  private renewNow(): void {
    this.lastRenewAt = Date.now();
    if (this.renewTimer !== null) clearTimeout(this.renewTimer);
    this.renewTimer = setTimeout(() => {
      this.renewTimer = null;
      this.renewNow();
    }, RENEW_INTERVAL_MS);
    api.renewSession().catch((err: unknown) => {
      if (!(err instanceof api.ApiError && err.status === 401)) console.warn('renew: не удалось', err);
    });
  }

  private renewVisibilityListener = (): void => {
    if (document.visibilityState !== 'visible') return;
    if (Date.now() - this.lastRenewAt > RENEW_INTERVAL_MS) this.renewNow();
  };

  // --- Ф10-7: входы (сессии) и доступ с телефона --------------------------------------------------------------

  async listSessions(): Promise<UiSession[]> {
    return (await api.listSessions()).sessions;
  }

  /** Выйти в одной сессии; своя текущая — кука уже удалена ответом, показываем экран входа. */
  async revokeSession(session: UiSession): Promise<void> {
    await api.revokeSession(session.id);
    if (session.current) this.endSession();
  }

  /** «Выйти везде» (`keepCurrent=false`) или «везде, кроме этого». Число отозванных. */
  async revokeAllSessions(keepCurrent: boolean): Promise<number> {
    const { revoked } = await api.revokeAllSessions(keepCurrent);
    if (!keepCurrent) this.endSession();
    return revoked;
  }

  private endSession(): void {
    this.stopBackground();
    api.setCsrf(null);
    this.state.screen = 'login';
  }

  // F1 (приёмка 6b, дефект): раньше `lastSeq` стартовал с 0, и первый `/sync` выкачивал ВСЮ историю всех
  // каналов в память (и слал уведомление на каждую строку при скрытом окне). Порядок строго:
  // status (max_seq) -> lastSeq -> подключить поток -> ОДИН явный /sync, закрывающий щель между шагами
  // -> загрузить проекты. `EventSource` без курсора и так стартует с текущего max_seq на сервере — здесь
  // мы лишь не даём КЛИЕНТУ считать «с нуля» до того, как поток встанет.
  private async afterLogin(): Promise<void> {
    this.state.screen = 'app';
    await this.primeLastSeqFromStatus();
    this.startBackground();
    this.renewNow(); // Ф10-7: сессия живёт, пока ей пользуются хотя бы раз в 30 дней
    void this.runSync(); // «лучше чуть перечитать, чем потерять событие между шагами» (коммент архитектора)
    await this.loadProjects();
  }

  private async primeLastSeqFromStatus(): Promise<void> {
    try {
      const status = await api.getStatus();
      this.state.status = status;
      if (status.max_seq > this.state.sync.lastSeq) {
        this.state.sync.lastSeq = status.max_seq;
      }
    } catch {
      // Не получили статус — переживём один большой /sync (не хуже прежнего поведения), но хотя бы попытались.
    }
  }

  // --- фоновые процессы: SSE + давность + системные события ------------------------------------------------

  private openStream(): void {
    this.stream?.close();
    this.stream = connectStream({
      url: api.streamUrl(),
      staleness: this.state.staleness,
      onSyncNeeded: () => {
        void this.runSync();
      },
      onOpen: () => {
        this.reconnectAttempt = 0; // поток снова жив — бэкофф с начала при следующем обрыве
      },
      onClosed: () => {
        void this.handleStreamClosed();
      },
      createEventSource: this.deps.createEventSource,
    });
  }

  private startBackground(): void {
    this.stopClock = startStalenessClock(this.state.staleness);
    this.openStream();
    // Побочная находка при правке F10: `visibilitychange` — событие `document`, не `window` (оно не
    // всплывает до `window`); `online`/`offline`, наоборот, — события именно `window`.
    document.addEventListener('visibilitychange', this.forceSyncListener);
    window.addEventListener('online', this.forceSyncListener);
    document.addEventListener('visibilitychange', this.safetyVisibilityListener);
    document.addEventListener('visibilitychange', this.renewVisibilityListener);
    this.scheduleSafetySync();
    // Ф8-3 D3: присутствие — пока окно видимо, в фокусе и открыта беседа.
    this.presence?.stop();
    this.presence = new PresenceTracker({
      post: api.postPresence,
      target: () => this.presenceTarget(),
      isActive: this.deps.presenceActive ?? defaultIsActive,
    });
    this.presence.start();
    // Ф20-3 A1: активность в хабе (статус Slack «На связи») — окно в фокусе и действие за 5 мин.
    this.activity?.stop();
    this.activity = new ActivityTracker({
      post: api.postPresenceActive,
      isActive: this.deps.presenceActive ?? defaultIsActive,
    });
    this.activity.start();
  }

  /** Открытая беседа и тред (панель треда открыта в ЭТОЙ беседе) — для присутствия (П8). Ф10-6 (решение 5):
   * пока открыта главная (на телефоне — «Ждёт», «Проекты», «Оркестратор»), беседу владелец не видит, даже если она
   * запомнена. */
  private presenceTarget(): { channelId: number; threadExtId: string | null } | null {
    if (this.state.homeOpen) {
      // Ф41-6б: на «Главной» (компьютер) владелец видит беседу активного столбца — без треда; нет такой — `null`.
      const active = this.state.boardActiveChannelId;
      return active === null || isPhone() ? null : { channelId: active, threadExtId: null };
    }
    const channelId = this.state.currentChannelId;
    if (channelId === null) return null;
    const open = this.state.openThread;
    return { channelId, threadExtId: open !== null && open.channelId === channelId ? open.threadExtId : null };
  }

  private stopBackground(): void {
    this.stopClock?.();
    this.stopClock = null;
    this.presence?.stop();
    this.presence = null;
    this.activity?.stop();
    this.activity = null;
    this.stream?.close();
    this.stream = null;
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    document.removeEventListener('visibilitychange', this.forceSyncListener);
    window.removeEventListener('online', this.forceSyncListener);
    document.removeEventListener('visibilitychange', this.safetyVisibilityListener);
    document.removeEventListener('visibilitychange', this.renewVisibilityListener);
    if (this.renewTimer !== null) {
      clearTimeout(this.renewTimer);
      this.renewTimer = null;
    }
    if (this.safetySyncTimer !== null) {
      clearTimeout(this.safetySyncTimer);
      this.safetySyncTimer = null;
    }
    if (this.syncRetryTimer !== null) {
      clearTimeout(this.syncRetryTimer);
      this.syncRetryTimer = null;
    }
    if (this.homeRefreshTimer !== null) {
      clearTimeout(this.homeRefreshTimer);
      this.homeRefreshTimer = null;
    }
    if (this.sessionEventsTimer !== null) {
      clearTimeout(this.sessionEventsTimer);
      this.sessionEventsTimer = null;
    }
    if (this.sessionPollTimer !== null) {
      clearInterval(this.sessionPollTimer);
      this.sessionPollTimer = null;
      this.sessionWatchers = 0;
    }
    this.closeLive();
    for (const timer of this.liveDoneTimers.values()) clearTimeout(timer);
    this.liveDoneTimers.clear();
  }

  // Ф8-1 A5: страховочный `/sync` по таймеру, независимо от SSE (пустой `/sync` дешёвый). Если поток молча
  // перестал приносить кадры (или кадр потерялся), лента всё равно догонит не позже чем через 30 с.
  private scheduleSafetySync(): void {
    if (this.safetySyncTimer !== null) clearTimeout(this.safetySyncTimer);
    const delay = document.visibilityState === 'visible' ? SAFETY_SYNC_VISIBLE_MS : SAFETY_SYNC_HIDDEN_MS;
    this.safetySyncTimer = setTimeout(() => {
      this.safetySyncTimer = null;
      this.scheduleSafetySync();
      void this.runSync();
    }, delay);
  }

  // Смена видимости меняет период страховочного таймера (скрытая вкладка — 2 мин, видимая — 30 с).
  private safetyVisibilityListener = (): void => {
    if (this.safetySyncTimer !== null) this.scheduleSafetySync();
  };

  private forceSyncListener = (): void => {
    // F10: было `||` — бессмысленно (почти всегда true). Синхронизируем только когда вкладка ВИДНА И есть сеть.
    if (document.visibilityState === 'visible' && navigator.onLine) {
      void this.runSync();
    }
  };

  // F2 (приёмка 6b, дефект): по спецификации EventSource фатальный HTTP-ответ (401 после истечения сессии,
  // 5xx/421 при рестарте core) НАВСЕГДА закрывает соединение (`readyState === CLOSED`) — браузер сам больше
  // не переподключается, обработчика не было вовсе. Здесь: сперва проверяем сессию (`authMe()` — если она
  // истекла, 401 сработает через `unauthorizedHandler` и переведёт экран сам, реконнект не нужен), иначе —
  // переподключение с бэкоффом и обязательный `/sync` (новый `EventSource` не несёт `Last-Event-ID`).
  private async handleStreamClosed(): Promise<void> {
    try {
      await api.authMe();
    } catch (err) {
      // Отступаем ТОЛЬКО когда сессии больше нет (экран «сессия истекла» уже показан обработчиком 401).
      // Сетевая ошибка или 5xx — это как раз перезапуск core: переподключаться обязательно, иначе лента
      // замерла бы навсегда (находка архитектора при чтении кода).
      if (err instanceof api.ApiError && err.status === 401) return;
    }
    const delay = RECONNECT_DELAYS_MS[Math.min(this.reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)];
    this.reconnectAttempt += 1;
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.openStream();
      void this.runSync();
    }, delay);
  }

  async runSync(): Promise<void> {
    if (this.syncing) {
      this.syncAgainAfter = true;
      return;
    }
    this.syncing = true;
    try {
      let more = true;
      while (more) {
        const res: SyncResponse = await api.sync(this.state.sync.lastSeq);
        this.applySyncBatch(res);
        if (res.messages.length > 0 || res.channels.length > 0) {
          this.scheduleProjectsRefresh();
          this.scheduleHomeRefresh();
        }
        more = res.more;
      }
      this.onSyncSucceeded();
      await this.refreshStatus();
    } catch (err) {
      this.onSyncFailed(err);
    } finally {
      this.syncing = false;
      if (this.syncAgainAfter) {
        this.syncAgainAfter = false;
        void this.runSync();
      }
    }
  }

  // Ф8-1 A3: одна «ядовитая» строка не должна останавливать ленту навсегда. Если целая пачка бросила —
  // применяем её же поканально и посообщенно, пропуская бросающие элементы, и всё равно сдвигаем `lastSeq`
  // до `max_seq` пачки (пропущенное дочитается REST-ом при открытии беседы). Повторное применение уже
  // применённой части безопасно — `applySync` идемпотентна (upsert по `id`).
  private applySyncBatch(res: SyncResponse): void {
    this.applySessionChanges(res.hub_sessions, res.session_requests);
    this.applyJoinChanges(res.joins);
    this.applyRuleRequestChanges(res.rule_requests);
    this.applyAgentRequestChanges(res.agent_requests);
    this.applyPressChanges(res.presses);
    this.applyHumanChanges(res.human_requests);
    this.applyReminderChanges(res.reminders);
    this.applyPeerMsgChanges(res.peer_msgs);
    this.applyLeaveChanges(res.leaves);
    this.applyGroupOpChanges(res.group_ops);
    this.onOwnerSeq(res.max_seq);
    // Ф8-4 B4: беседа удалена у себя в хабе (здесь или в другой вкладке) — открытая закрывается.
    for (const channel of res.channels) {
      if (channel.hidden) this.onChannelGone(channel.id);
    }
    try {
      applySync(this.state.sync, res);
      return;
    } catch (err) {
      console.error('sync: пачка не применилась целиком, применяю поштучно', err);
    }
    for (const channel of res.channels) {
      try {
        applySync(this.state.sync, { channels: [channel], messages: [], max_seq: 0 });
      } catch (err) {
        console.error(`sync: канал ${channel.id} пропущен`, err);
      }
    }
    for (const message of res.messages) {
      try {
        applySync(this.state.sync, { channels: [], messages: [message], max_seq: 0 });
      } catch (err) {
        console.error(`sync: сообщение ${message.id} пропущено`, err);
      }
    }
    if (res.max_seq > this.state.sync.lastSeq) {
      this.state.sync.lastSeq = res.max_seq;
    }
  }

  private onSyncSucceeded(): void {
    this.state.syncError = null;
    this.syncRetryAttempt = 0;
    if (this.syncRetryTimer !== null) {
      clearTimeout(this.syncRetryTimer);
      this.syncRetryTimer = null;
    }
  }

  // Ф8-1 A4: раньше любая ошибка цикла глоталась молча — лента замирала без следа. Теперь: консоль,
  // `syncError` для верхней полосы и повтор с бэкоффом. 401 — как раньше: экран «сессия истекла» уже
  // показан глобальным обработчиком, фоновые процессы остановлены, повторять нечего.
  private onSyncFailed(err: unknown): void {
    if (err instanceof api.ApiError && err.status === 401) return;
    if (this.state.screen !== 'app') return;
    console.error('sync: не удалось', err);
    this.state.syncError = { at: Date.now(), message: err instanceof Error ? err.message : String(err) };
    if (this.syncRetryTimer !== null) return; // повтор уже запланирован
    const delay = SYNC_RETRY_DELAYS_MS[Math.min(this.syncRetryAttempt, SYNC_RETRY_DELAYS_MS.length - 1)];
    this.syncRetryAttempt += 1;
    this.syncRetryTimer = setTimeout(() => {
      this.syncRetryTimer = null;
      void this.runSync();
    }, delay);
  }

  // F4 (приёмка 6b): счётчики проектов (`unread` в `GET /projects`) не пересчитываются сами по себе —
  // перезапрашиваем их после пачки `/sync`, где реально были изменения, с троттлингом ≥ 1с (не долбим
  // ручку на каждый мелкий кадр потока).
  private scheduleProjectsRefresh(): void {
    const now = Date.now();
    const elapsed = now - this.lastProjectsRefreshAt;
    if (elapsed >= PROJECTS_REFRESH_MIN_INTERVAL_MS) {
      this.lastProjectsRefreshAt = now;
      void this.loadProjects();
      return;
    }
    if (this.projectsRefreshTimer !== null) return;
    this.projectsRefreshTimer = setTimeout(() => {
      this.projectsRefreshTimer = null;
      this.lastProjectsRefreshAt = Date.now();
      void this.loadProjects();
    }, PROJECTS_REFRESH_MIN_INTERVAL_MS - elapsed);
  }

  // --- данные -----------------------------------------------------------------------------------------------

  async loadProjects(): Promise<void> {
    this.state.projects = await api.listProjects();
  }

  // F4: список каналов проекта больше НЕ хранится отдельной REST-копией — он вычисляется из
  // `state.sync.channelsById` (см. `store/selectors.ts`), которую `/sync` держит свежей сама. Здесь только
  // прогреваем эту карту первым запросом и переключаем текущий проект.
  //
  // Ф10-6 B3 (решение 4): вкладка проекта помнит беседу — `openProject` открывает запомненную, если она ещё
  // видна в проекте по `sync.channelsById` (скрытая или перенесённая в другой проект — не открывается).
  // `restore: false` — вызывающий сам откроет нужную беседу (поиск, очередь, клик на главной).
  async openProject(projectId: number, opts: { restore?: boolean } = {}): Promise<void> {
    // Ф11-4 И5а: закрываемые беседа/тред оставили бы свою запись истории телефона сверху.
    const closing =
      isPhone() && (this.state.currentChannelId !== null || this.state.openThread !== null) && this.topPhoneLevel() !== null;
    this.closeThread();
    this.state.homeOpen = false;
    this.state.claudeOpen = false;
    if (isPhone()) this.state.phoneTab = 'chats';
    this.state.currentProjectId = projectId;
    this.state.currentChannelId = null;
    this.presence?.update();
    const remembered = opts.restore === false ? undefined : this.state.lastChannelByProject[projectId];
    if (remembered !== undefined && this.channelVisibleIn(projectId, remembered)) {
      void this.openChannel(remembered); // займёт оставшуюся запись (`openChannel`), новой не добавит
    } else if (closing && opts.restore !== false) {
      // Беседу никто не откроет — снять осиротевшую запись: иначе первое «Назад» ничего не закроет. (При
      // `restore: false` беседу сразу откроет вызывающий — она займёт запись.)
      try {
        window.history.back();
      } catch {
        // истории нет — нечего снимать
      }
    }
    const channels = await api.listChannels(projectId);
    for (const channel of channels) {
      this.state.sync.channelsById[channel.id] = channel;
    }
  }

  private channelVisibleIn(projectId: number, channelId: number): boolean {
    const channel = this.state.sync.channelsById[channelId];
    if (channel === undefined || channel.hidden === true) return false;
    return channelsOfProject(this.state.sync, projectId).some((c) => c.id === channelId);
  }

  async openChannel(channelId: number, options: { thread?: string | null } = {}): Promise<void> {
    // Ф10-6 B4 (решение 8): на телефоне открытие беседы при закрытой — новая запись истории («Назад» её
    // закроет); смена беседы при открытой историю не растит.
    // Ф11-4 И5а: наша запись истории сверху при закрытой беседе — осталась от беседы/треда, закрытых без «Назад»
    // (`openProject` с доски закрывает их сам) — занимаем её, а не добавляем новую: лишнее «Назад» ничего не закрыло бы.
    if (isPhone()) {
      if (this.state.currentChannelId === null) {
        if (this.topPhoneLevel() !== null) this.replacePhoneLevel('channel');
        else this.pushPhoneLevel('channel');
      }
      this.state.phoneTab = 'chats';
    }
    // Спека 2e: смена канала закрывает панель треда (открыт максимум один тред за раз, привязанный к
    // конкретному каналу — переход в другой канал делает его контекст бессмысленным).
    this.closeThread();
    this.state.homeOpen = false;
    this.state.claudeOpen = false;
    this.state.currentChannelId = channelId;
    if (this.state.currentProjectId !== null) this.state.lastChannelByProject[this.state.currentProjectId] = channelId;
    this.presence?.update(); // Ф8-3 D3: смена беседы — сразу новое присутствие
    // Ф66-5: сразу панель свежего треда (лента под ней грузится параллельно и прокручивается к родителю).
    const thread = options.thread ?? null;
    if (thread !== null && thread !== '') {
      await Promise.all([this.loadChannelPage(channelId), this.openThread(channelId, thread)]);
      return;
    }
    await this.loadChannelPage(channelId);
  }

  /** Ф41-6б (2.4): загрузить последнюю страницу сообщений беседы БЕЗ смены экрана — `currentChannelId`, `homeOpen`,
   * `openThread` и история телефона не трогаются (столбец беседы на доске). Тикет — та же защита от устаревшего
   * ответа, что у `openChannel`. */
  async loadChannelPage(channelId: number): Promise<void> {
    const ticket = (this.channelOpenTicket[channelId] ?? 0) + 1;
    this.channelOpenTicket[channelId] = ticket;
    this.state.loadingMessagesFor[channelId] = true;
    this.trimToLastPage(channelId); // Ф23-1 п.3: переоткрытие — последние 30, не вся накопленная портянка
    try {
      const page = await api.listMessages(channelId, { limit: MESSAGE_PAGE });
      mergeMessagePage(this.state.sync, page.messages); // безопасно применить и для устаревшего ответа
      if (this.channelOpenTicket[channelId] !== ticket) return; // более новый вызов для этой беседы уже в полёте
      this.state.nextBefore[channelId] = page.next_before;
    } finally {
      if (this.channelOpenTicket[channelId] === ticket) {
        this.state.loadingMessagesFor[channelId] = false;
      }
    }
  }

  /** Ф41-6: запись беседы для столбца доски, если её ещё нет в сторе (после перезагрузки страницы). Сбой — молча. */
  async ensureChannel(channelId: number): Promise<void> {
    if (this.state.sync.channelsById[channelId] !== undefined) return;
    try {
      const channel = await api.getChannel(channelId);
      if (channel !== null && typeof channel === 'object' && this.state.sync.channelsById[channelId] === undefined) {
        this.state.sync.channelsById[channelId] = channel;
      }
    } catch {
      // название появится, когда запись придёт обычным путём
    }
  }

  /** Ф41-6б: тред из столбца беседы — беседа открывается обычным видом на вкладке проекта с этим тредом
   * (панели треда в столбце нет). */
  async openThreadInProject(channelId: number, threadExtId: string): Promise<void> {
    await this.openChannelInProject(channelId);
    await this.openThread(channelId, threadExtId);
  }

  /** Ф41-6б: «все беседы <проекта>» из столбца — вкладка проекта с этой беседой. */
  async openChannelInProject(channelId: number): Promise<void> {
    const projectId = this.state.sync.channelsById[channelId]?.project_id ?? null;
    await this.openByProjectAndChannel(projectId !== null && projectId > 0 ? projectId : null, channelId);
  }

  /** Ф41-6б (2.4): беседа активного столбца «Главной» (указатель над ней или фокус внутри) — для присутствия. */
  setBoardActiveChannel(channelId: number, active: boolean): void {
    if (active) this.state.boardActiveChannelId = channelId;
    else if (this.state.boardActiveChannelId === channelId) this.state.boardActiveChannelId = null;
    this.presence?.update();
  }

  /** Ф23-1 п.3: у беседы в сторе могли накопиться все догруженные страницы — при открытии оставляем последние
   * `MESSAGE_PAGE`; курсор «выше» берётся из свежей страницы (она те же последние 30). Цель фокуса глубже —
   * не режем. */
  private trimToLastPage(channelId: number): void {
    const list = this.state.sync.messagesByChannel[channelId];
    if (list === undefined || list.length <= MESSAGE_PAGE) return;
    const cut = list.length - MESSAGE_PAGE;
    const focus = this.state.focus;
    if (focus !== null && focus.channelId === channelId) {
      const idx = list.findIndex((m) =>
        focus.messageId !== null ? m.id === focus.messageId : focus.outboxId !== null && m.outbox_id === focus.outboxId,
      );
      if (idx !== -1 && idx < cut) return;
    }
    list.splice(0, cut);
    this.state.nextBefore[channelId] = null;
  }

  async loadOlderMessages(channelId: number): Promise<void> {
    if (this.loadingOlder.has(channelId)) return; // F6: защита от параллельных вызовов (частый scroll)
    const before = this.state.nextBefore[channelId];
    if (before === null || before === undefined) return;
    this.loadingOlder.add(channelId);
    try {
      const page = await api.listMessages(channelId, { before, limit: MESSAGE_PAGE });
      mergeMessagePage(this.state.sync, page.messages);
      this.state.nextBefore[channelId] = page.next_before;
    } finally {
      this.loadingOlder.delete(channelId);
    }
  }

  async loadInbox(): Promise<void> {
    this.state.inbox = await api.listInbox();
  }

  // --- панель треда (спека 2e) -------------------------------------------------------------------------

  /** Открыть тред (сводка под родителем в ленте, кнопка «Треды», клик «Ответить» в Slack-канале). Заводит
   * запись в `sync.threads`, если её ещё не было, грузит первую страницу ответов. Тикет — та же защита от
   * гонки устаревшего ответа, что у `openChannel`/`channelOpenTicket`: быстрый повторный клик по другому
   * треду/каналу не даёт устаревшей странице перезаписать уже актуальное состояние. */
  async openThread(channelId: number, threadExtId: string): Promise<void> {
    if (isPhone() && this.state.openThread === null) this.pushPhoneLevel('thread');
    this.state.openThread = { channelId, threadExtId };
    this.presence?.update(); // Ф8-3 D3/П8: панель треда — присутствие с `thread_ext_id`
    const key = threadKey(channelId, threadExtId);
    const existing = this.state.sync.threads[key];
    if (existing === undefined) {
      this.state.sync.threads[key] = { parent: null, replies: [], nextBefore: null, loading: true };
    } else {
      existing.loading = true;
    }
    const ticket = (this.threadOpenTicket[key] ?? 0) + 1;
    this.threadOpenTicket[key] = ticket;
    try {
      const page = await api.listThreadMessages(channelId, threadExtId, { limit: MESSAGE_PAGE });
      if (this.threadOpenTicket[key] !== ticket) return; // устарело — открыли другой тред, пока грузили
      mergeThreadMessagePage(this.state.sync.threads, channelId, threadExtId, page);
    } finally {
      if (this.threadOpenTicket[key] === ticket) {
        const thread = this.state.sync.threads[key];
        if (thread !== undefined) thread.loading = false;
      }
    }
  }

  closeThread(): void {
    this.state.openThread = null;
    this.presence?.update();
  }

  /** Ф10-6 B3: закрыть беседу (телефон: «‹» ленты — назад к списку бесед). Проект остаётся открытым. */
  closeChannel(): void {
    this.closeThread();
    this.state.currentChannelId = null;
    this.presence?.update();
  }

  // --- телефон: нижние вкладки и «Назад» (Ф10-6 B4) --------------------------------------------------------

  /** `chats` — беседы (нет выбранного проекта — «Все»); остальные — главная БЕЗ сброса выбранных проекта и
   * беседы: вернулся в «Беседы» — там же. */
  setPhoneTab(tab: PhoneTab): void {
    if (this.state.phoneSession !== null && tab !== 'orch') this.closePhoneSession();
    this.state.phoneTab = tab;
    this.state.claudeOpen = false;
    if (tab === 'chats') {
      this.state.homeOpen = false;
      if (this.state.currentProjectId === null) {
        void this.openProject(ALL_PROJECTS_ID);
        return;
      }
    } else {
      this.state.homeOpen = true;
    }
    this.presence?.update();
  }

  private pushPhoneLevel(level: PhoneLevel): void {
    try {
      window.history.pushState({ [PHONE_HISTORY_KEY]: level }, '');
    } catch {
      // История недоступна — «Назад» браузера просто не закроет уровень, «‹» закроет сам.
    }
  }

  private replacePhoneLevel(level: PhoneLevel): void {
    try {
      window.history.replaceState({ [PHONE_HISTORY_KEY]: level }, '');
    } catch {
      // История недоступна — как у `pushPhoneLevel`.
    }
  }

  private topPhoneLevel(): PhoneLevel | null {
    try {
      const state: unknown = window.history.state;
      if (state !== null && typeof state === 'object') {
        const level = (state as Record<string, unknown>)[PHONE_HISTORY_KEY];
        if (level === 'channel' || level === 'thread' || level === 'session') return level;
      }
    } catch {
      // нет истории — нет и своих записей
    }
    return null;
  }

  /** «‹» в интерфейсе телефона: верхняя запись истории — наша для этого уровня -> `history.back()` (закроет
   * `popstate`), иначе закрыть самому. Вне телефона — сразу закрыть. */
  goBack(level: PhoneLevel): void {
    if (isPhone() && this.topPhoneLevel() === level) {
      window.history.back();
      return;
    }
    if (level === 'thread') this.closeThread();
    else if (level === 'session') this.state.phoneSession = null;
    else this.closeChannel();
  }

  /** Ф41-6б (раздел 3): открыть сессию/диалог агента/оркестратор на весь экран вкладки «Сессии» (уровень истории). */
  openPhoneSession(item: ColumnItem): void {
    if (this.state.phoneSession === null) this.pushPhoneLevel('session');
    this.state.phoneSession = item;
    if (item.kind === 'session' && item.id !== undefined) {
      this.state.sessions.expanded[item.id] = true;
      void this.refreshSession(item.id);
    }
  }

  /** Закрыть полноэкранный уровень «Сессии»; запись истории, если она наша, снимается (без повторного закрытия). */
  closePhoneSession(): void {
    const item = this.state.phoneSession;
    this.state.phoneSession = null;
    if (item?.kind === 'session' && item.id !== undefined) this.state.sessions.expanded[item.id] = false;
    if (this.topPhoneLevel() === 'session') {
      try {
        window.history.back();
      } catch {
        // истории нет — нечего снимать
      }
    }
  }

  /** Показать страницу «Агенты» (десктоп) или её вкладку на телефоне. Ф78-1: задач по «Го» нет — только сессия. Ф41-6a: переход к СЕССИИ (`sessionId`)
   * на компьютере ведёт на «Главную» со столбцом этой сессии. */
  revealProject(projectId: number, sessionId: number | null = null): void {
    if (sessionId !== null && !isPhone()) {
      this.openHome();
      openColumn({ kind: 'session', id: sessionId });
    } else if (isPhone()) {
      // Ф41-6б: к сессии — вкладка «Сессии» с этой сессией на весь экран; иначе — страница «Агенты».
      if (sessionId !== null) {
        if (this.state.phoneTab !== 'orch') this.setPhoneTab('orch');
        this.openPhoneSession({ kind: 'session', id: sessionId });
      } else if (this.state.phoneTab !== 'dispatch') {
        this.setPhoneTab('dispatch');
      }
    } else if (!this.state.claudeOpen) {
      this.openClaude();
    }
    this.boardFocusSeq += 1;
    this.state.boardFocus = { projectId, sessionId, seq: this.boardFocusSeq };
  }

  /** Ф41-2б-2: ждущая карточка сессии — окно сессии раскрыто; компьютер — столбец на «Главной», телефон — вкладка «Claude». */
  revealSession(projectId: number, sessionId: number): void {
    if (this.state.sessions.expanded[sessionId] !== true) this.toggleSessionWindow(sessionId);
    this.revealProject(projectId, sessionId);
  }

  /** Ф20-4 B1: открыть вкладку «Claude» (десктоп; на телефоне — нижняя вкладка «Claude»). */
  openClaude(): void {
    if (isPhone()) {
      this.setPhoneTab('dispatch');
      return;
    }
    this.closeThread();
    this.state.homeOpen = false;
    this.state.claudeOpen = true;
    this.state.currentProjectId = null;
    this.state.currentChannelId = null;
    this.presence?.update();
    void this.loadHome();
    void this.loadSessions();
    void this.loadJoins();
    void this.loadLeaves();
    void this.loadGroupOps();
    void this.loadPeerMsgs();
  }

  /** `popstate` телефона: закрыть верхний уровень — тред, иначе беседу. Вне телефона историю не трогаем. */
  onPhonePopState(): void {
    if (!isPhone()) return;
    if (this.state.phoneSession !== null) {
      const item = this.state.phoneSession;
      this.state.phoneSession = null;
      if (item.kind === 'session' && item.id !== undefined) this.state.sessions.expanded[item.id] = false;
    } else if (this.state.openThread !== null) this.closeThread();
    else if (this.state.currentChannelId !== null) this.closeChannel();
  }

  async loadOlderReplies(): Promise<void> {
    const open = this.state.openThread;
    if (open === null) return;
    const key = threadKey(open.channelId, open.threadExtId);
    if (this.loadingOlderReplies.has(key)) return; // F6 (как у ленты): защита от параллельных вызовов
    const before = this.state.sync.threads[key]?.nextBefore;
    if (before === null || before === undefined) return;
    this.loadingOlderReplies.add(key);
    try {
      const page = await api.listThreadMessages(open.channelId, open.threadExtId, { before, limit: MESSAGE_PAGE });
      mergeThreadMessagePage(this.state.sync.threads, open.channelId, open.threadExtId, page);
    } finally {
      this.loadingOlderReplies.delete(key);
    }
  }

  async markThreadRead(channelId: number, threadExtId: string, upToMessageId: number): Promise<void> {
    await api.markThreadRead(channelId, threadExtId, upToMessageId);
  }

  async listThreads(channelId: number, opts: { unread?: boolean } = {}): Promise<ThreadListItem[]> {
    return api.listChannelThreads(channelId, opts);
  }

  async refreshStatus(): Promise<void> {
    this.state.status = await api.getStatus();
  }

  messagesOf(channelId: number): Message[] {
    return this.state.sync.messagesByChannel[channelId] ?? [];
  }

  /** F8: клик по результату поиска ИЛИ по элементу очереди outbox — открыть его проект (псевдо-проект
   * «Неразобранное» для `project_id=null`) и канал, а не только канал (иначе колонки проектов/каналов не
   * подсвечивают, где мы оказались). */
  private async openByProjectAndChannel(
    projectId: number | null,
    channelId: number,
    threadExtId: string | null = null,
  ): Promise<void> {
    const resolvedProjectId = projectId ?? UNSORTED_PROJECT_ID;
    if (this.state.currentProjectId !== resolvedProjectId || this.state.homeOpen) {
      await this.openProject(resolvedProjectId, { restore: false });
    }
    await this.openChannel(channelId, { thread: threadExtId });
  }

  /** Ф14-2 D2: в «Кому» выбрана существующая беседа — открыть её (проект + канал), заготовка не заводится. */
  async openRecipientChannel(projectId: number | null, channelId: number): Promise<void> {
    await this.openByProjectAndChannel(projectId, channelId);
  }

  async openSearchResult(result: SearchResult): Promise<void> {
    await this.openByProjectAndChannel(result.project_id, result.channel_id);
  }

  async openOutboxQueueItem(item: OutboxListItem): Promise<void> {
    await this.openByProjectAndChannel(item.project_id, item.channel_id);
  }

  // --- главная (Ф8-6, раздел C) -----------------------------------------------------------------------------

  /** Открыть главную; `anchor` — раздел, к которому прокрутить («ждёт: N» -> `drafts`). */
  openHome(anchor: HomeAnchor | null = null): void {
    this.closeThread();
    this.state.homeOpen = true;
    this.state.claudeOpen = false;
    if (isPhone() && this.state.phoneTab === 'chats') this.state.phoneTab = 'wait';
    this.state.homeAnchor = anchor;
    this.state.currentProjectId = null;
    this.state.currentChannelId = null;
    this.presence?.update();
    void this.loadHome();
    void this.loadSessions();
    void this.loadJoins();
    void this.loadLeaves();
    void this.loadGroupOps();
    void this.loadPeerMsgs();
  }

  /** Клик на главной: открыть беседу (проект + канал) и прокрутить к сообщению или строке черновика. */
  async openChannelAt(
    projectId: number | null,
    channelId: number,
    target: { messageId?: number | null; outboxId?: number | null; threadExtId?: string | null } = {},
    options: { column?: boolean } = {},
  ): Promise<void> {
    this.focusSeq += 1;
    this.state.focus = {
      channelId,
      messageId: target.messageId ?? null,
      outboxId: target.outboxId ?? null,
      threadExtId: target.threadExtId ?? null,
      seq: this.focusSeq,
    };
    // Ф66-5: цель — сообщение внутри треда: беседа открывается обычным видом проекта сразу с панелью треда.
    if (target.threadExtId) {
      await this.openByProjectAndChannel(projectId, channelId, target.threadExtId);
      return;
    }
    // Клик на «Главной» компьютера: беседа открывается окном (для ознакомления); столбцом — только из списка
    // слева (`options.column`) или если столбец с ней уже есть на доске (тогда он подсвечивается).
    if (this.state.homeOpen && !isPhone()) {
      const item: ColumnItem = { kind: 'channel', id: channelId };
      if (options.column === true || boardState.layout.open.some((o) => sameItem(o, item))) {
        this.state.channelModalId = null;
        openColumn(item);
      } else {
        this.state.channelModalOverlay = false;
        this.state.channelModalId = channelId;
      }
      await this.loadChannelPage(channelId);
      return;
    }
    await this.openByProjectAndChannel(projectId, channelId);
  }

  /** Закрыть окно беседы; активность беседы окно снимает само (`ChannelModal`). */
  closeChannelModal(): void {
    this.state.channelModalId = null;
    this.state.channelModalOverlay = false;
  }

  /** Ф46-1: беседа окном поверх текущего экрана компьютера (вкладка проекта, список бесед, «Главная»): маршрут и
   * открытая беседа под окном не меняются, закрытие возвращает как было. Телефон — вызывающий идёт обычным переходом. */
  async openChannelOverlay(channelId: number): Promise<void> {
    this.state.channelModalOverlay = true;
    this.state.channelModalId = channelId;
    await this.loadChannelPage(channelId).catch(() => undefined);
  }

  /** Окно беседы -> столбец доски («На доску»). */
  channelModalToColumn(): void {
    const id = this.state.channelModalId;
    if (id === null) return;
    this.state.channelModalId = null;
    this.state.channelModalOverlay = false;
    openColumn({ kind: 'channel', id });
  }

  /** Снять цель прокрутки. С `channelId` — только если цель этой беседы (столбец чужую не сбрасывает). */
  clearFocus(channelId?: number): void {
    if (channelId !== undefined && this.state.focus?.channelId !== channelId) return;
    this.state.focus = null;
  }

  /** Все разделы главной разом: «Нужна реакция», черновики, запуски Claude и отправленное сегодня. Раздел,
   * который не загрузился, не стирает остальные — ошибка показывается строкой. */
  async loadHome(nowMs: number = Date.now()): Promise<void> {
    const since = startOfDay(nowMs);
    const [react, drafts, runs, sent, queued] = await Promise.allSettled([
      api.getHomeReact(),
      api.listOutbox('pending_approval', 200),
      api.listAgentRuns({ since, limit: 200 }),
      api.listOutbox('sent', 200, since),
      api.listOutbox('queued', 200),
    ]);
    const home = this.state.home;
    if (react.status === 'fulfilled') {
      // Ф34-2: строки-напоминания (`reminder` не null, `last` null) в список бесед не попадают — они живут в
      // `state.reminders`; иначе код, читающий `item.last.ts`, на них упадёт.
      const rows = asList<HomeReactItem>(react.value);
      home.react = rows.filter((row) => !row.reminder && row.last);
      for (const row of rows) if (row.reminder) applyReminder(this.state.reminders, row.reminder);
    }
    if (drafts.status === 'fulfilled') home.drafts = asList<OutboxListItem>(drafts.value);
    if (runs.status === 'fulfilled') home.runs = asList<AgentRun>(runs.value);
    if (sent.status === 'fulfilled') home.sent = asList<OutboxListItem>(sent.value);
    if (queued.status === 'fulfilled') home.scheduled = scheduledOf(queued.value);
    this.pruneDraftSelection();
    const failed = [react, drafts, runs, sent, queued].some((r) => r.status === 'rejected');
    home.error = failed ? 'Часть главной не загрузилась — повторю при следующем обновлении.' : null;
    home.loadedAt = nowMs;
  }

  /** Ф37-1 A4: разрешить/запретить беседе отправку без подтверждения для роли. Ошибка 422 — пробрасывается с
   * текстом `detail`; список беседы обновляет `/sync`. */
  async setChannelAutosend(channelId: number, role: string, on: boolean): Promise<void> {
    await api.putChannelAutosend(channelId, role, on);
    await this.runSync();
  }

  /** Только черновики — для закреплённых над полем ввода беседы (C2), когда главная закрыта. */
  async loadDrafts(): Promise<void> {
    try {
      this.state.home.drafts = asList<OutboxListItem>(await api.listOutbox('pending_approval', 200));
      this.pruneDraftSelection();
    } catch {
      // Не загрузились — закреплённые черновики останутся прежними до следующего обновления.
    }
  }

  // --- Ф12-2 C: выбор черновиков флажками и подтверждение пачкой -----------------------------------------------

  isDraftSelected(outboxId: number): boolean {
    return this.state.draftSelection.includes(outboxId);
  }

  /** Флажок черновика: выбор — в конец очереди выбора (порядок выбора = порядок слотов графика). */
  toggleDraftSelection(outboxId: number): void {
    const selection = this.state.draftSelection;
    this.state.draftSelection = selection.includes(outboxId)
      ? selection.filter((id) => id !== outboxId)
      : [...selection, outboxId];
  }

  /** «Выбрать все»: ещё не выбранные — в конец, в порядке списка. */
  selectDrafts(outboxIds: number[]): void {
    const selection = this.state.draftSelection;
    this.state.draftSelection = [...selection, ...outboxIds.filter((id) => !selection.includes(id))];
  }

  /** Выбранные черновики из списка `outboxIds` — в порядке выбора. */
  selectedDraftsOf(outboxIds: number[]): number[] {
    return this.state.draftSelection.filter((id) => outboxIds.includes(id));
  }

  /** Выбор держит только ещё ждущие подтверждения черновики. */
  private pruneDraftSelection(): void {
    const pending = new Set(this.state.home.drafts.map((d) => d.id));
    if (this.state.draftSelection.some((id) => !pending.has(id))) {
      this.state.draftSelection = this.state.draftSelection.filter((id) => pending.has(id));
    }
  }

  /** «Подтвердить выбранные»: одним запросом, по порядку выбора; подтверждённые уходят из списков, ошибки —
   * по пунктам в итоге (черновик с ошибкой остаётся выбранным). */
  async approveOutboxBatch(outboxIds: number[]): Promise<ApproveBatchResult[]> {
    const { results } = await api.approveOutboxBatch(outboxIds);
    const done = new Set(results.filter((r) => r.status === 'queued').map((r) => r.id));
    this.state.home.drafts = this.state.home.drafts.filter((d) => !done.has(d.id));
    this.state.draftSelection = this.state.draftSelection.filter((id) => !done.has(id));
    void this.runSync();
    void (this.state.homeOpen || this.state.claudeOpen ? this.loadHome() : this.loadDrafts());
    return results;
  }

  draftsOf(channelId: number): OutboxListItem[] {
    return this.state.home.drafts
      .filter((d) => d.channel_id === channelId)
      .sort((a, b) => a.created_ts - b.created_ts || a.id - b.id);
  }

  // C1: при изменениях `/sync` — не чаще раза в 2 с; главная закрыта — только черновики (закреплённые в беседе).
  private scheduleHomeRefresh(): void {
    const run = (): void => {
      this.lastHomeRefreshAt = Date.now();
      void (this.state.homeOpen || this.state.claudeOpen ? this.loadHome() : this.loadDrafts());
    };
    const elapsed = Date.now() - this.lastHomeRefreshAt;
    if (elapsed >= HOME_REFRESH_MIN_INTERVAL_MS) {
      run();
      return;
    }
    if (this.homeRefreshTimer !== null) return;
    this.homeRefreshTimer = setTimeout(() => {
      this.homeRefreshTimer = null;
      run();
    }, HOME_REFRESH_MIN_INTERVAL_MS - elapsed);
  }

  // --- постоянные сессии хаба (Ф41-2б-2) -------------------------------------------------------------------

  /** Записи и запросы из `/sync`; события и очередь реплик раскрытых окон — дочитать. Ядовитая строка не мешает. */
  private applySessionChanges(items: HubSession[] | undefined, requests: SessionRequest[] | undefined): void {
    const st = this.state.sessions;
    for (const session of Array.isArray(items) ? items : []) {
      try {
        upsertSession(st, session);
        const cur = st.items[session.id];
        if (cur === undefined || st.expanded[session.id] !== true) continue;
        if ((cur.last_event_id ?? 0) > lastSessionEventId(st, cur.id)) this.scheduleSessionEvents(cur.id);
        if ((st.says[cur.id]?.length ?? 0) !== cur.queued_says || (st.pending[cur.id]?.length ?? 0) !== cur.pending_requests) {
          void this.refreshSession(cur.id);
        }
      } catch (err) {
        console.error('sync: сессия хаба пропущена', err);
      }
    }
    for (const request of Array.isArray(requests) ? requests : []) {
      try {
        applySessionRequest(st, request);
      } catch (err) {
        console.error('sync: запрос сессии хаба пропущен', err);
      }
    }
  }

  /** Список незакрытых сессий (`GET /sessions`): первичная загрузка и опрос раз в 30 с. */
  async loadSessions(nowMs: number = Date.now()): Promise<void> {
    const st = this.state.sessions;
    try {
      const body = await api.listHubSessions();
      replaceSessions(st, asList<HubSession>(body?.items));
      st.today = body?.today != null && typeof body.today.cost_usd === 'number' ? body.today : null;
      st.error = null;
      st.loadedAt = nowMs;
    } catch (err) {
      console.error('сессии: список не загрузился', err);
      st.error = 'Сессии не загрузились — повторю при следующем обновлении.';
    }
  }

  /** Пока смонтирован блок сессий — список живёт сам (раз в 30 с при видимой странице). Возвращает снятие. */
  watchSessions(): () => void {
    this.sessionWatchers += 1;
    if (this.sessionPollTimer === null) {
      void this.loadSessions();
      this.sessionPollTimer = setInterval(() => {
        if (document.visibilityState !== 'hidden') void this.loadSessions();
      }, SESSION_POLL_MS);
    }
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.sessionWatchers -= 1;
      if (this.sessionWatchers <= 0 && this.sessionPollTimer !== null) {
        clearInterval(this.sessionPollTimer);
        this.sessionPollTimer = null;
        this.sessionWatchers = 0;
      }
    };
  }

  /** Ф41-3а: подробности шага (diff, вывод, вход инструмента) — один запрос на событие, дальше из стора. */
  async loadSessionEventDetail(sessionId: number, eventId: number): Promise<void> {
    const st = this.state.sessions;
    const key = detailKey(sessionId, eventId);
    const cur = st.details[key];
    if (cur !== undefined && cur.status !== 'error') return;
    st.details[key] = { status: 'loading', detail: null };
    try {
      const full = await api.getHubSessionEvent(sessionId, eventId);
      st.details[key] = { status: 'ok', detail: full?.detail ?? null };
    } catch (err) {
      console.error(`сессии: подробности события ${eventId} не загрузились`, err);
      st.details[key] = { status: 'error', detail: null };
    }
  }

  /** Ф41-3б: пока открыто хотя бы одно окно сессии, держим ОДНО соединение с потоком набираемого текста
   * (`/sessions/live`); вкладка скрыта — соединения нет. Возвращает снятие. */
  watchSessionLive(): () => void {
    this.liveWatchers += 1;
    if (this.liveVisibilityListener === null) {
      this.liveVisibilityListener = () => this.syncLiveConnection();
      document.addEventListener('visibilitychange', this.liveVisibilityListener);
    }
    this.syncLiveConnection();
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.liveWatchers = Math.max(0, this.liveWatchers - 1);
      this.syncLiveConnection();
      if (this.liveWatchers === 0 && this.liveVisibilityListener !== null) {
        document.removeEventListener('visibilitychange', this.liveVisibilityListener);
        this.liveVisibilityListener = null;
      }
    };
  }

  private syncLiveConnection(): void {
    const want = this.liveWatchers > 0 && document.visibilityState !== 'hidden';
    if (!want) {
      this.closeLive();
      return;
    }
    if (this.liveSource === null && this.liveReconnectTimer === null) this.openLive();
  }

  private openLive(): void {
    let source: LiveSourceLike;
    const url = api.sessionsLiveUrl();
    if (this.deps.createLiveEventSource !== undefined) {
      source = this.deps.createLiveEventSource(url);
    } else if (typeof EventSource !== 'undefined') {
      source = new EventSource(url) as unknown as LiveSourceLike;
    } else {
      return;
    }
    this.liveSource = source;
    source.addEventListener('live', (ev) => this.onLiveFrame(ev));
    // Ф60-1C: кадры помощника на интервью идут тем же потоком (второго соединения нет).
    source.addEventListener('interview', (ev) => applyInterviewFrame(ev.data));
    // Новое соединение (в том числе после обрыва): кадры за разрыв потеряны — запись перечитывается.
    if (interviewUi.loaded) void loadInterviewCurrent();
    source.addEventListener('error', () => {
      if (this.liveSource !== source) return;
      // Обрыв: закрыть и переподключиться через паузу (браузерный авто-реконнект не ждёт и не знает про 403).
      source.close();
      this.liveSource = null;
      if (this.liveWatchers > 0 && this.liveReconnectTimer === null) {
        this.liveReconnectTimer = setTimeout(() => {
          this.liveReconnectTimer = null;
          this.syncLiveConnection();
        }, LIVE_RECONNECT_MS);
      }
    });
  }

  private closeLive(): void {
    if (this.liveSource !== null) {
      this.liveSource.close();
      this.liveSource = null;
    }
    if (this.liveReconnectTimer !== null) {
      clearTimeout(this.liveReconnectTimer);
      this.liveReconnectTimer = null;
    }
  }

  private onLiveFrame(ev: MessageEvent): void {
    let data: unknown;
    try {
      data = JSON.parse(String(ev.data));
    } catch {
      return;
    }
    if (data === null || typeof data !== 'object') return;
    const f = data as Record<string, unknown>;
    if (typeof f.session_id !== 'number' || typeof f.block !== 'string' || typeof f.text !== 'string') return;
    const st = this.state.sessions;
    const sessionId = f.session_id;
    const frame = {
      block: f.block,
      kind: f.kind === 'thinking' ? ('thinking' as const) : ('text' as const),
      text: f.text,
      cut: f.cut === true,
      done: f.done === true,
    };
    const prevTimer = this.liveDoneTimers.get(sessionId);
    if (prevTimer !== undefined) {
      clearTimeout(prevTimer);
      this.liveDoneTimers.delete(sessionId);
    }
    if (!applyLiveFrame(st, sessionId, frame)) return;
    if (frame.done) {
      // Строка держится ещё 5 с после `done` — окончательный `text` обычно успевает прийти и заменить её сам.
      this.liveDoneTimers.set(
        sessionId,
        setTimeout(() => {
          this.liveDoneTimers.delete(sessionId);
          if (st.live[sessionId]?.block === frame.block) delete st.live[sessionId];
        }, LIVE_DONE_LINGER_MS),
      );
    }
  }

  /** Запись сессии с её ожидающими запросами и очередью реплик (`GET /sessions/{id}`). */
  async refreshSession(id: number): Promise<void> {
    const st = this.state.sessions;
    try {
      const detail = await api.getHubSession(id);
      upsertSession(st, detail.session);
      if (st.items[id] === undefined) return;
      setSessionPending(st, id, Array.isArray(detail.requests) ? detail.requests : []);
      setSessionSays(st, id, Array.isArray(detail.says) ? detail.says : []);
    } catch (err) {
      console.error(`сессии: сессия ${id} не перечиталась`, err);
    }
  }

  /** Раскрыть/свернуть окно сессии; при раскрытии — перечитать запись (события грузит сама лента при показе). */
  toggleSessionWindow(id: number): void {
    const st = this.state.sessions;
    st.expanded[id] = !st.expanded[id];
    if (st.expanded[id]) void this.refreshSession(id);
  }

  private scheduleSessionEvents(id: number): void {
    this.sessionEventsDirty.add(id);
    if (this.sessionEventsTimer !== null) return;
    const flush = (): void => {
      this.lastSessionEventsAt = Date.now();
      const ids = [...this.sessionEventsDirty];
      this.sessionEventsDirty.clear();
      for (const sid of ids) void this.loadSessionEvents(sid);
    };
    const elapsed = Date.now() - this.lastSessionEventsAt;
    if (elapsed >= WORK_EVENTS_MIN_INTERVAL_MS) {
      flush();
      return;
    }
    this.sessionEventsTimer = setTimeout(() => {
      this.sessionEventsTimer = null;
      flush();
    }, WORK_EVENTS_MIN_INTERVAL_MS - elapsed);
  }

  /** Первая загрузка — последние 30 событий (`before`), дальше — новые после загруженного (`after`). */
  async loadSessionEvents(id: number): Promise<void> {
    if (this.sessionEventsLoading.has(id)) {
      this.sessionEventsDirty.add(id);
      return;
    }
    const st = this.state.sessions;
    this.sessionEventsLoading.add(id);
    try {
      if (st.events[id] === undefined) {
        const last = st.items[id]?.last_event_id ?? null;
        if (last === null) {
          st.events[id] = [];
          st.olderDone[id] = true;
          return;
        }
        const page = asList<SessionEvent>(await api.listHubSessionEvents(id, { before: last + 1, limit: WORK_EVENTS_PAGE }));
        mergeSessionEvents(st, id, page);
        st.olderDone[id] = page.length < WORK_EVENTS_PAGE;
      }
      for (;;) {
        const page = asList<SessionEvent>(
          await api.listHubSessionEvents(id, { after: lastSessionEventId(st, id), limit: WORK_EVENTS_AFTER_LIMIT }),
        );
        mergeSessionEvents(st, id, page);
        if (page.length < WORK_EVENTS_AFTER_LIMIT) break;
      }
    } catch (err) {
      console.error(`сессии: события сессии ${id} не загрузились`, err);
    } finally {
      this.sessionEventsLoading.delete(id);
      if (this.sessionEventsDirty.has(id)) this.scheduleSessionEvents(id);
    }
  }

  /** «Показать раньше»: следующие 30 событий до первого загруженного. */
  async loadOlderSessionEvents(id: number): Promise<void> {
    const st = this.state.sessions;
    const first = firstSessionEventId(st, id);
    if (first === null || st.olderDone[id]) return;
    try {
      const page = asList<SessionEvent>(await api.listHubSessionEvents(id, { before: first, limit: WORK_EVENTS_PAGE }));
      mergeSessionEvents(st, id, page);
      if (page.length < WORK_EVENTS_PAGE) st.olderDone[id] = true;
    } catch (err) {
      console.error(`сессии: ранние события сессии ${id} не загрузились`, err);
    }
  }

  /** Действие владельца над сессией: отказ ядра (409/410/422) — готовая русская строка из `detail`. После любого
   * исхода запись перечитывается (очередь реплик, просьбы «прервать»/«усыпить»). */
  private async sessionAction(id: number | null, fn: () => Promise<unknown>): Promise<SessionActionResult> {
    try {
      await fn();
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 410 || err.status === 422)) {
        return { ok: false, stale: err.status !== 422, message: err.message };
      }
      console.error('сессии: действие не прошло', err);
      return { ok: false, stale: false, message: 'Не отправилось — попробуй ещё раз.' };
    } finally {
      if (id !== null) await this.refreshSession(id);
      void this.runSync();
    }
  }

  async saySession(
    id: number,
    text: string,
    priority: 'next' | 'now',
    files?: string[],
  ): Promise<SessionActionResult> {
    const result = await this.sessionAction(id, () => api.sayHubSession(id, text, priority, files));
    // Ф41-3в: реплика ушла — приложенные к ней чипы убираются; не ушла — остаются (можно отправить ещё раз).
    if (result.ok && files !== undefined && files.length > 0) {
      const list = this.state.sessions.files[id];
      if (list !== undefined) this.state.sessions.files[id] = list.filter((f) => f.fileId === null || !files.includes(f.fileId));
    }
    return result;
  }

  /** Ф41-3в: добавить файлы к следующей реплике и грузить их в ядро (`POST /sessions/{id}/files`). Больше
   * `SESSION_FILES_MAX` за раз не берём — остальное отбрасывается, возвращается строка-пояснение. */
  attachSessionFiles(sessionId: number, files: File[]): string | null {
    const st = this.state.sessions;
    let note: string | null = null;
    for (const file of files) {
      const list = st.files[sessionId] ?? [];
      if (list.length >= SESSION_FILES_MAX) {
        note = `Не больше ${SESSION_FILES_MAX} файлов в одной реплике.`;
        break;
      }
      const localId = ++this.sessionFileSeq;
      const name = file.name || 'file';
      st.files[sessionId] = [
        ...list,
        { localId, name, size: file.size, kind: null, status: 'uploading', fileId: null, error: null },
      ];
      const controller = new AbortController();
      this.sessionFileUploads.set(localId, controller);
      void this.uploadSessionDraft(sessionId, localId, file, name, controller);
    }
    return note;
  }

  private patchSessionFile(sessionId: number, localId: number, patch: Partial<SessionFileDraft>): boolean {
    const list = this.state.sessions.files[sessionId];
    const idx = list?.findIndex((f) => f.localId === localId) ?? -1;
    if (list === undefined || idx === -1) return false;
    list[idx] = { ...list[idx]!, ...patch };
    return true;
  }

  private async uploadSessionDraft(
    sessionId: number,
    localId: number,
    file: File,
    name: string,
    controller: AbortController,
  ): Promise<void> {
    try {
      const result = await api.uploadSessionFile(sessionId, file, name, controller.signal);
      if (!this.patchSessionFile(sessionId, localId, { fileId: result.id, name: result.name, size: result.size, kind: result.kind, status: 'ready' })) {
        // Убрали крестиком, пока ответ был в пути, — файл на стороне ядра не нужен.
        void api.deleteSessionFile(sessionId, result.id).catch(() => undefined);
      }
    } catch (err) {
      let message = 'не загрузился';
      if (err instanceof api.ApiError && err.status === 413) message = 'файл больше 20 МБ';
      else if (err instanceof api.ApiError && (err.status === 409 || err.status === 410 || err.status === 422)) message = err.message;
      else if (!controller.signal.aborted) console.error('сессии: файл не загрузился', err);
      this.patchSessionFile(sessionId, localId, { status: 'error', error: message });
    } finally {
      this.sessionFileUploads.delete(localId);
    }
  }

  /** Ф41-3в: «×» на чипе — отменить загрузку или убрать загруженный файл (`DELETE`); с чипа снимается в любом случае. */
  async removeSessionFile(sessionId: number, localId: number): Promise<void> {
    const st = this.state.sessions;
    const item = st.files[sessionId]?.find((f) => f.localId === localId);
    if (item === undefined) return;
    st.files[sessionId] = (st.files[sessionId] ?? []).filter((f) => f.localId !== localId);
    this.sessionFileUploads.get(localId)?.abort();
    this.sessionFileUploads.delete(localId);
    if (item.fileId !== null) {
      try {
        await api.deleteSessionFile(sessionId, item.fileId);
      } catch (err) {
        console.error('сессии: файл не удалился (ядро уберёт сама через сутки)', err);
      }
    }
  }

  /** Ф41-4: правка желаемой подписки (`PATCH`); ответ ядра — запись целиком. */
  async patchSessionSub(id: number, wantSub: SessionWantSub | null): Promise<SessionActionResult> {
    const result = await this.sessionAction(id, async () => {
      const { session } = await api.patchHubSession(id, { want_sub: wantSub });
      upsertSession(this.state.sessions, session);
    });
    return result;
  }

  wakeSession(id: number): Promise<SessionActionResult> {
    return this.sessionAction(id, () => api.wakeHubSession(id));
  }

  mcpReloadSession(id: number): Promise<SessionActionResult> {
    return this.sessionAction(id, () => api.mcpReloadHubSession(id));
  }

  compactSession(id: number): Promise<SessionActionResult> {
    return this.sessionAction(id, () => api.compactHubSession(id));
  }

  interruptSession(id: number): Promise<SessionActionResult> {
    return this.sessionAction(id, () => api.interruptHubSession(id));
  }

  sleepSession(id: number): Promise<SessionActionResult> {
    return this.sessionAction(id, () => api.sleepHubSession(id));
  }

  async closeSession(id: number): Promise<SessionActionResult> {
    const result = await this.sessionAction(id, () => api.closeHubSession(id));
    if (result.ok) {
      const st = this.state.sessions;
      const cur = st.items[id];
      if (cur !== undefined) upsertSession(st, { ...cur, closed_ts: Date.now() });
    }
    return result;
  }

  /** Отписать внешнюю сессию от хаба; список агентов перечитывается сразу. 404 — подписки уже нет (тоже успех). */
  async endSub(subId: number): Promise<SessionActionResult> {
    try {
      await api.endSub(subId);
    } catch (err) {
      if (!(err instanceof api.ApiError && err.status === 404)) {
        console.error('подписка: не отписана', err);
        return { ok: false, stale: false, message: 'Не получилось отписать — попробуй ещё раз.' };
      }
    }
    // Не ждём: окно, в котором нажали, должно успеть сообщить «готово» до того, как строка уйдёт из списка.
    void this.loadAgentSubs();
    return { ok: true };
  }

  /** Ф64-1: «Закончить день» / «Начать день»; проекты перечитываются сразу (события `/sync` для смены дня нет).
   * Ошибка — текстом ответа ядра (409 `workday_off` и др.). */
  async setProjectDay(projectId: number, open: boolean): Promise<{ ok: true } | { ok: false; message: string }> {
    try {
      await api.setProjectDay(projectId, open);
    } catch (err) {
      return { ok: false, message: err instanceof api.ApiError ? err.message : 'Не получилось сменить день — попробуй ещё раз.' };
    }
    try {
      await this.loadProjects();
    } catch (err) {
      console.error('проекты: не перечитаны после смены дня', err);
    }
    // Ф70-1: пометка «канал спит» — из `GET /status`; перечитать сразу, не дожидаясь следующего `/sync`.
    void this.refreshStatus().catch(() => undefined);
    return { ok: true };
  }

  /** Ф41-5: переключатель «Ручной | Авто» (`PATCH /sessions/{id} {mode}`). */
  async setSessionMode(id: number, mode: 'manual' | 'auto'): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      const { session } = await api.patchHubSession(id, { mode });
      upsertSession(this.state.sessions, session);
    });
  }

  /** Ф41-7: «Модель и усилие…» (`PATCH /sessions/{id} {model, effort}`; `null` — умолчание движка). */
  async setSessionModel(id: number, model: string | null, effort: string | null): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      const { session } = await api.patchHubSession(id, { model, effort });
      upsertSession(this.state.sessions, session);
    });
  }

  /** Ф83: запасной движок сессии (`PATCH /sessions/{id} {fallback}`: `null` — как у проекта, `{engine: null}` — нет). */
  async setSessionFallback(id: number, fallback: api.FallbackPatch): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      const { session } = await api.patchHubSession(id, { fallback });
      upsertSession(this.state.sessions, session);
    });
  }

  /** Ф83: «Вернуть на основной»; 409 «<движок> на паузе до ЧЧ:ММ» — текст ядра в `message`. */
  async returnSessionFallback(id: number): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      const { session } = await api.returnHubSessionFallback(id);
      upsertSession(this.state.sessions, session);
    });
  }

  /** Ф83: «Перейти на <запасной>» у паузы — тот же ответ `fallback`, что у карточки `paused:{id}`. */
  async switchSessionFallback(id: number): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      await api.answerOwner(`paused:${id}`, { action: 'fallback' });
      void this.loadOwner();
    });
  }

  /** Ф48-2 D: «Сжатие…» (`PATCH /sessions/{id} {compact_tokens, compact_auto}` — только изменённые ключи). */
  async setSessionCompact(id: number, body: { compact_tokens?: number | null; compact_auto?: boolean }): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      const { session } = await api.patchHubSession(id, body);
      upsertSession(this.state.sessions, session);
    });
  }

  /** Ф99-7.2: снять «Всегда в этой сессии» (только с интерфейса — ядро принимает лишь куку). */
  async removeSessionAlways(id: number, key: string): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      const { session } = await api.removeHubSessionAlways(id, key);
      upsertSession(this.state.sessions, session);
    });
  }

  /** Ф48-2 A: «Отправить снова» прерванную реплику. */
  async resendSessionSay(id: number, sayId: number): Promise<SessionActionResult> {
    return this.sessionAction(id, () => notFoundAsStale(() => api.resendHubSessionSay(id, sayId)));
  }

  /** Ф48-2 A: «Убрать» прерванную реплику. */
  async dropSessionSay(id: number, sayId: number): Promise<SessionActionResult> {
    return this.sessionAction(id, () => notFoundAsStale(() => api.dropHubSessionSay(id, sayId)));
  }

  /** Смена роли существующей сессии (`role: null` — снять); применится со следующего входа. */
  async setSessionRole(
    id: number,
    body: { role: string | null; number?: number; autosend_ok?: boolean; role_file?: string | null },
  ): Promise<SessionActionResult> {
    let warning: string | undefined;
    const result = await this.sessionAction(id, async () => {
      const reply = await api.patchHubSession(id, body);
      upsertSession(this.state.sessions, reply.session);
      warning = reply.role_file_warning;
    });
    return result.ok && warning ? { ok: true, warning } : result;
  }

  /** Только галка «без подтверждения» (роль не менялась). */
  async setSessionAutosend(id: number, autosendOk: boolean): Promise<SessionActionResult> {
    return this.sessionAction(id, async () => {
      const { session } = await api.patchHubSession(id, { autosend_ok: autosendOk });
      upsertSession(this.state.sessions, session);
    });
  }

  /** Ф41-7: «Новая с передачей дел» — прежняя закрывается, на её место на доске встаёт новая. */
  async handoffSession(id: number, input: HandoffInput, column?: ColumnItem): Promise<SessionActionResult> {
    try {
      const { session, closed } = await api.handoffHubSession(id, input);
      this.state.handoffTo[closed?.id ?? id] = session.id;
      const phone = this.state.phoneSession;
      if (phone !== null && phone.kind === 'session' && phone.id === id) this.state.phoneSession = { kind: 'session', id: session.id };
      if (closed) upsertSession(this.state.sessions, closed);
      upsertSession(this.state.sessions, session);
      replaceColumn(column ?? { kind: 'session', id }, { kind: 'session', id: session.id });
      void this.refreshSession(session.id);
      void this.runSync();
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 410 || err.status === 422)) {
        return { ok: false, stale: false, message: err.message };
      }
      console.error('сессии: передача дел не прошла', err);
      return { ok: false, stale: false, message: 'Не получилось передать дела — попробуй ещё раз.' };
    }
  }

  /** Новая сессия. Отказ ядра (занятая линия, потолок) уходит вызывающему — форма показывает `message`. */
  async createSession(input: SessionCreateInput, firstText = ''): Promise<SessionActionResult> {
    try {
      const { session, role_file_warning: warning, resume_warning: resumeWarning } = await api.createHubSession(input);
      upsertSession(this.state.sessions, session);
      void this.refreshSession(session.id);
      void this.runSync();
      // Первое задание из формы: сессия уже заведена, поэтому неудача реплики — не отказ формы, а пояснение.
      if (firstText !== '') {
        const said = await this.saySession(session.id, firstText, 'next');
        if (!said.ok) {
          const note = warning ? ` И ещё: ${warning}.` : '';
          return { ok: false, stale: false, message: `Сессия заведена, но задание не ушло — напиши его в окне сессии.${note}` };
        }
      }
      return {
        ok: true,
        ...(warning ? { warning } : {}),
        ...(resumeWarning ? { resumeWarning } : {}),
      };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 410 || err.status === 422)) {
        return { ok: false, stale: false, message: err.message };
      }
      console.error('сессии: сессия не создана', err);
      return { ok: false, stale: false, message: 'Не создана — попробуй ещё раз.' };
    }
  }

  /** Решение по запросу сессии; 409 — запрос протух: помечается и уходит из ожидающих (карточка предлагает реплику). */
  async decideSessionRequest(
    request: SessionRequest,
    body: { decision: 'allow' | 'deny'; answers?: Record<string, string>; message?: string; always?: boolean },
  ): Promise<WorkActionResult> {
    const result = await this.sessionAction(request.session_id, () => api.decideHubSessionRequest(request.id, body));
    const st = this.state.sessions;
    if (!result.ok && !result.stale) return 'error';
    if (!result.ok) st.stale[request.id] = request;
    const list = st.pending[request.session_id];
    if (list !== undefined) st.pending[request.session_id] = list.filter((r) => r.id !== request.id);
    return result.ok ? 'ok' : 'stale';
  }

  dismissStaleSessionRequest(requestId: number): void {
    delete this.state.sessions.stale[requestId];
  }

  // --- напоминания по беседам (Ф34-2) ---------------------------------------------------------------------------

  /** Напоминания из `/sync`: старшая по `seq` побеждает; ядовитая строка не мешает остальным. */
  private applyReminderChanges(items: Reminder[] | undefined): void {
    for (const reminder of Array.isArray(items) ? items : []) {
      try {
        applyReminder(this.state.reminders, reminder);
      } catch (err) {
        console.error('sync: напоминание пропущено', err);
      }
    }
  }

  /** Ждущие и сработавшие напоминания беседы (или всё открытое) — при открытии беседы и главной. */
  async loadReminders(channelId?: number): Promise<void> {
    const reminders = this.state.reminders;
    try {
      const body = await api.listReminders({ channelId, limit: 200 });
      for (const reminder of asList<Reminder>(body?.items)) applyReminder(reminders, reminder);
      reminders.error = null;
    } catch {
      reminders.error = 'Напоминания не загрузились — повторю при следующем обновлении.';
    }
  }

  /** Поставить напоминание из UI; ошибка (422/429/409) уходит вызывающему — текст `detail` показывает форма. */
  async createReminder(input: api.ReminderInput): Promise<Reminder> {
    const fresh = await api.createReminder(input);
    if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyReminder(this.state.reminders, fresh);
    void this.runSync();
    return fresh;
  }

  /** «Отложить» / «Перенести» (`at`), «Готово» / «Снять» (`close`). 409 (уже закрыто) — перечитать. */
  async patchReminder(
    reminder: Reminder,
    fields: api.ReminderPatch,
  ): Promise<WorkActionResult> {
    try {
      const fresh = await api.patchReminder(reminder.id, fields);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyReminder(this.state.reminders, fresh);
      return 'ok';
    } catch (err) {
      if (err instanceof api.ApiError && err.status === 409) {
        await this.loadReminders(reminder.channel_id ?? undefined);
        return 'stale';
      }
      console.error('напоминание: правка не прошла', err);
      return 'error';
    } finally {
      void this.runSync();
    }
  }

  // --- сообщения агентов и диалог владельца с агентом (Ф35-3) ---------------------------------------------------------

  /** Сообщения агентов из `/sync`: старший `seq` побеждает; ядовитая строка не мешает остальным. */
  private applyPeerMsgChanges(items: PeerMsg[] | undefined): void {
    if (this.agentWatchers > 0 && Array.isArray(items) && items.length > 0) void this.refreshAgentSubs();
    for (const msg of Array.isArray(items) ? items : []) {
      try {
        applyPeerMsg(this.state.peerMsgs, msg);
      } catch (err) {
        console.error('sync: сообщение агентов пропущено', err);
      }
    }
  }

  /** Последние сообщения агентов (новые первыми); `before` — догрузка старых («ещё»). */
  async loadPeerMsgs(before?: number): Promise<void> {
    const st = this.state.peerMsgs;
    try {
      const body = await api.listPeerMsgs(before);
      const list = asList<PeerMsg>(body?.items);
      for (const msg of list) applyPeerMsg(st, msg);
      if (before !== undefined) st.more = list.length > 0;
      st.error = null;
    } catch {
      st.error = 'Сообщения агентов не загрузились — повторю при следующем обновлении.';
    }
  }

  /** Агенты в сети: строки проектных подписок из `GET /subs?live=1`. */
  async loadAgentSubs(): Promise<void> {
    try {
      const body = await api.listSubs();
      this.state.peerMsgs.agentSubs = asList<ProjectSub>(body?.items);
    } catch {
      this.state.peerMsgs.error = 'Агенты не загрузились — повторю при следующем обновлении.';
    }
  }

  /** Ф36-2 B1: перечитать агентов не чаще раза в `AGENT_REFRESH_MIN_MS` (сигнал `/sync`, возврат страницы, таймер). */
  private agentWatchers = 0;
  private agentRefreshAt = 0;
  async refreshAgentSubs(): Promise<void> {
    const now = Date.now();
    if (now - this.agentRefreshAt < AGENT_REFRESH_MIN_MS) return;
    this.agentRefreshAt = now;
    await this.loadAgentSubs();
  }

  /** Ф36-2 B1: пока вкладка «Claude» смонтирована — список агентов живёт сам: раз в 30 с при видимой странице,
   *  при возврате из фона и когда `/sync` принёс сообщения агентов. Возвращает функцию снятия таймера и слушателя. */
  watchAgentSubs(): () => void {
    this.agentWatchers += 1;
    const timer = setInterval(() => {
      if (document.visibilityState !== 'hidden') void this.refreshAgentSubs();
    }, AGENT_POLL_MS);
    const onVisible = (): void => {
      if (document.visibilityState === 'visible') void this.refreshAgentSubs();
    };
    document.addEventListener('visibilitychange', onVisible);
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.agentWatchers -= 1;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }

  /** Написать агенту; ошибка ядра уходит вызывающему (текст `detail` показывает диалог, введённое не теряется).
   *  Отказ — повод перечитать список агентов: адресат мог уйти из сети, его номер — занять другая сессия. */
  async sendToAgent(text: string, to: AgentAddress): Promise<PeerSendResult> {
    let res: PeerSendResult;
    try {
      res = await api.sendPeerMsg({ text, to });
    } catch (err) {
      void this.loadAgentSubs();
      throw err;
    }
    if (res?.msg !== undefined && typeof res.msg.id === 'number') applyPeerMsg(this.state.peerMsgs, res.msg);
    void this.runSync();
    return res;
  }

  /** Открытый диалог: непрочитанные ответы линии — прочитаны (только строки `to_user`). */
  async markAgentRepliesRead(key: string, subId?: number | null): Promise<void> {
    const ids = unreadReplies(this.state.peerMsgs.items, key, subId).map((m) => m.id);
    if (ids.length === 0) return;
    try {
      await api.markPeerMsgsRead(ids);
      for (const id of ids) {
        const msg = this.state.peerMsgs.items[id];
        if (msg !== undefined && msg.status === 'pending') msg.status = 'read';
      }
    } catch (err) {
      console.error('агенты: отметка прочитанного не прошла', err);
    }
  }

  // --- заявки на вступление (Ф17-1b) ---------------------------------------------------------------------------

  /** Заявки из `/sync`: старшая по `seq` побеждает; ядовитая строка не мешает остальным. */
  private applyJoinChanges(joins: JoinRequest[] | undefined): void {
    for (const join of Array.isArray(joins) ? joins : []) {
      try {
        applyJoin(this.state.joins, join);
      } catch (err) {
        console.error('sync: заявка на вступление пропущена', err);
      }
    }
  }

  /** Открытые заявки и закрытые за 7 дней (`GET /joins`) — при открытии главной и раз в 30 с. */
  async loadJoins(): Promise<void> {
    const joins = this.state.joins;
    try {
      for (const join of asList<JoinRequest>(await api.listJoins({ limit: 200 }))) applyJoin(joins, join);
      joins.error = null;
    } catch {
      joins.error = 'Заявки на вступление не загрузились — повторю при следующем обновлении.';
    }
  }

  /** «Вступить» / «Отказать»: 409 (уже решена или уже вступаю) — заявка перечитана. */
  async decideJoin(join: JoinRequest, decision: 'approve' | 'decline'): Promise<WorkActionResult> {
    try {
      const fresh = decision === 'approve' ? await api.approveJoin(join.id) : await api.declineJoin(join.id);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyJoin(this.state.joins, fresh);
      return 'ok';
    } catch (err) {
      if (err instanceof api.ApiError && err.status === 409) {
        await this.loadJoins();
        return 'stale';
      }
      console.error('заявка на вступление: решение не прошло', err);
      return 'error';
    } finally {
      void this.runSync();
    }
  }

  // --- заявки на правило раскладки (Ф38-2) -----------------------------------------------------------------------

  /** Заявки из `/sync`: старшая по `seq` побеждает; ядовитая строка не мешает остальным. */
  private applyRuleRequestChanges(requests: RuleRequest[] | undefined): void {
    for (const request of Array.isArray(requests) ? requests : []) {
      try {
        applyRuleRequest(this.state.ruleRequests, request);
      } catch (err) {
        console.error('sync: заявка на правило раскладки пропущена', err);
      }
    }
  }

  /** Открытые заявки и решённые за 7 дней (`GET /rule-requests`) — при открытии главной и раз в 30 с. */
  async loadRuleRequests(): Promise<void> {
    const state = this.state.ruleRequests;
    try {
      for (const request of asList<RuleRequest>(await api.listRuleRequests({ limit: 200 }))) applyRuleRequest(state, request);
      state.error = null;
    } catch {
      state.error = 'Заявки на правила раскладки не загрузились — повторю при следующем обновлении.';
    }
  }

  /** «Завести» / «Отклонить». 409 «заявка уже решена» — заявка перечитана, `'stale'`; 409 «правило не завести: …» —
   * заявка остаётся ждущей, текст ядра показывается под кнопками (`{ detail }`). */
  async decideRuleRequest(
    request: RuleRequest,
    decision: 'approve' | 'decline',
  ): Promise<WorkActionResult | { detail: string }> {
    try {
      const fresh = decision === 'approve' ? await api.approveRuleRequest(request.id) : await api.declineRuleRequest(request.id);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') {
        applyRuleRequest(this.state.ruleRequests, fresh);
      }
      return 'ok';
    } catch (err) {
      if (err instanceof api.ApiError && err.status === 409) {
        if (err.message.startsWith('правило не завести')) return { detail: err.message };
        await this.loadRuleRequests();
        return 'stale';
      }
      console.error('заявка на правило раскладки: решение не прошло', err);
      return 'error';
    } finally {
      void this.runSync();
    }
  }

  // --- заявки агентов (Ф41-5) -------------------------------------------------------------------------------------

  private applyAgentRequestChanges(requests: AgentRequest[] | undefined): void {
    for (const request of Array.isArray(requests) ? requests : []) {
      try {
        applyAgentRequest(this.state.agentRequests, request);
      } catch (err) {
        console.error('sync: заявка агента пропущена', err);
      }
    }
  }

  // --- нажатия кнопок ботов (Ф44-1d) -----------------------------------------------------------------------------

  /** Нажатия из `/sync` (только кука; с ответом бота): старшая по `seq` побеждает; ядовитая строка не мешает остальным. */
  private applyPressChanges(presses: ButtonPress[] | undefined): void {
    for (const press of Array.isArray(presses) ? presses : []) {
      try {
        applyPress(this.state.presses, press);
      } catch (err) {
        console.error('sync: нажатие кнопки пропущено', err);
      }
    }
  }

  /** Живые и недавние нажатия (`GET /button-presses`, по умолчанию живые и изменившиеся за сутки). */
  async loadPresses(): Promise<void> {
    const state = this.state.presses;
    try {
      for (const press of asList<ButtonPress>(await api.listButtonPresses({ limit: 200 }))) applyPress(state, press);
      state.error = null;
    } catch (err) {
      // ручки нет в старом ядре (404) — нажатий просто нет
      if (!(err instanceof api.ApiError && err.status === 404)) {
        state.error = 'Нажатия кнопок не загрузились — повторю при следующем обновлении.';
      }
    }
  }

  /** Клик владельца по кнопке-действию. Повтор по живой строке безопасен (ядро вернёт ту же). Отказ — строкой исхода
   * под клавиатурой (`errors`): 409 `stale_button` — «клавиатура изменилась». */
  async pressButton(messageId: number, row: number, col: number, text: string): Promise<void> {
    const state = this.state.presses;
    const key = pressKey(messageId, row, col);
    delete state.errors[key];
    state.sending[key] = true;
    try {
      const reply = await api.pressButton(messageId, { row, col, text });
      if (reply?.press && typeof reply.press.id === 'number') applyPress(state, reply.press);
    } catch (err) {
      if (err instanceof api.ApiError && err.status === 409 && err.message.startsWith('stale_button')) {
        state.errors[key] = 'клавиатура изменилась — этой кнопки уже нет';
      } else if (err instanceof api.ApiError && (err.status === 404 || err.status === 422)) {
        state.errors[key] = `не нажато: ${err.message}`;
      } else {
        console.error('нажатие кнопки не прошло', err);
        state.errors[key] = 'не отправилось — попробуй ещё раз';
      }
    } finally {
      delete state.sending[key];
      void this.runSync();
    }
  }

  /** «Нажать» / «Отказать» у карточки. 409 — уже решено: строка перечитана, `'stale'`. */
  async decidePress(press: ButtonPress, decision: 'approve' | 'decline'): Promise<WorkActionResult> {
    try {
      const fresh = decision === 'approve' ? await api.approveButtonPress(press.id) : await api.declineButtonPress(press.id);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyPress(this.state.presses, fresh);
      return 'ok';
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 404)) {
        await this.loadPresses();
        return 'stale';
      }
      console.error('нажатие кнопки: решение не прошло', err);
      return 'error';
    } finally {
      void this.runSync();
    }
  }

  // --- заявки «нужен человек» (Ф54-1) ---------------------------------------------------------------------------

  private applyHumanChanges(items: HumanRequest[] | undefined): void {
    for (const item of Array.isArray(items) ? items : []) {
      try {
        applyHumanRequest(this.state.humans, item);
      } catch (err) {
        console.error('sync: заявка «нужен человек» пропущена', err);
      }
    }
  }

  /** Ждущие и недавно закрытые заявки (`GET /human-requests`). Ручки нет в старом ядре (404) — заявок просто нет. */
  async loadHumanRequests(): Promise<void> {
    const state = this.state.humans;
    try {
      for (const item of asList<HumanRequest>(await api.listHumanRequests({ limit: 200 }))) applyHumanRequest(state, item);
      state.error = null;
    } catch (err) {
      if (!(err instanceof api.ApiError && err.status === 404)) {
        state.error = 'Заявки «нужен человек» не загрузились — повторю при следующем обновлении.';
      }
    }
  }

  /** «Готово» / «Не вышло» / «Не сейчас». 409 — уже закрыта (`detail` в `message`), 404 — нет такой: перечитано. */
  async decideHuman(
    request: HumanRequest,
    outcome: 'done' | 'failed' | 'later',
  ): Promise<{ ok: true } | { ok: false; stale: boolean; message: string }> {
    try {
      const fresh = await api.decideHumanRequest(request.id, outcome);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyHumanRequest(this.state.humans, fresh);
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 404)) {
        // Ф65-1: 409 `errand_open` больше нет — ответ владельца закрывает поручение агента сам.
        await this.loadHumanRequests();
        return { ok: false, stale: true, message: err.message };
      }
      console.error('заявка «нужен человек»: решение не прошло', err);
      return { ok: false, stale: false, message: 'Не отправилось — попробуй ещё раз.' };
    } finally {
      void this.runSync();
    }
  }

  // --- очередь владельца: окно «Агент» (Ф56-3) ------------------------------------------------------------------------

  private ownerWatchers = 0;
  private ownerLoadAt = 0;
  private ownerTimer: ReturnType<typeof setTimeout> | null = null;
  private ownerErrandTimer: ReturnType<typeof setTimeout> | null = null;
  private humanErrandsSeen: string | null = null;

  /** `GET /owner_channel/queue` — полный снимок. Ручки нет в старом ядре (404) — очередь просто пуста. */
  async loadOwner(): Promise<void> {
    const state = this.state.owner_channel;
    state.loading = true;
    this.ownerLoadAt = Date.now();
    try {
      applyOwnerQueue(state, await api.getOwnerQueue());
      // Ф58-3: поручения по заявкам «нужен человек» изменились (поручили, прогресс, вернул, забрали) — карточки
      // заявок на главной и в «Ждёт» берут их из выдачи `GET /human-requests`: перечитать её.
      const mark = humanErrandsMark(state.items);
      if (mark !== this.humanErrandsSeen) {
        const first = this.humanErrandsSeen === null;
        this.humanErrandsSeen = mark;
        if (!first) void this.loadHumanRequests();
      }
    } catch (err) {
      if (!(err instanceof api.ApiError && err.status === 404)) {
        state.error = 'Очередь не загрузилась — повторю при следующем обновлении.';
      }
    } finally {
      state.loading = false;
      this.scheduleErrandPoll();
    }
  }

  /** Правки Ф65-1 15.10: строка хода поручения («думаю, 24 с…») в `/sync` и поток не попадает (кадра у очереди
   *  владельца нет) — пока у карточки открыто поручение агента, очередь перечитывается раз в `OWNER_ERRAND_POLL_MS`.
   *  Поручений нет — опрос стоит. Вкладка скрыта — не грузим, ждём следующего круга. */
  private scheduleErrandPoll(): void {
    const open = this.state.owner_channel.items.some((i) => i.errand !== null && i.errand !== undefined);
    if (!open) {
      if (this.ownerErrandTimer !== null) {
        clearTimeout(this.ownerErrandTimer);
        this.ownerErrandTimer = null;
      }
      return;
    }
    if (this.ownerErrandTimer !== null) return;
    this.ownerErrandTimer = setTimeout(() => {
      this.ownerErrandTimer = null;
      if (document.visibilityState === 'hidden') this.scheduleErrandPoll();
      else void this.loadOwner();
    }, OWNER_ERRAND_POLL_MS);
  }

  /** `/sync` принёс новое (`max_seq` ушёл дальше снимка): окно открыто или очередь уже грузили — перечитать, не чаще
   *  раза в `OWNER_REFRESH_MIN_MS`. */
  private onOwnerSeq(maxSeq: number): void {
    const state = this.state.owner_channel;
    if (!state.loaded || maxSeq <= state.seq) return;
    const wait = OWNER_REFRESH_MIN_MS - (Date.now() - this.ownerLoadAt);
    if (wait <= 0) {
      void this.loadOwner();
    } else if (this.ownerTimer === null) {
      this.ownerTimer = setTimeout(() => {
        this.ownerTimer = null;
        void this.loadOwner();
      }, wait);
    }
  }

  /** Пока окно «Агент» смонтировано: сразу грузит и раз в минуту перечитывает (сроки). Возвращает функцию снятия. */
  watchOwner(): () => void {
    this.ownerWatchers += 1;
    void this.loadOwner();
    const timer = setInterval(() => {
      if (document.visibilityState !== 'hidden') void this.loadOwner();
    }, OWNER_POLL_MS);
    let stopped = false;
    return () => {
      if (stopped) return;
      stopped = true;
      this.ownerWatchers -= 1;
      clearInterval(timer);
    };
  }

  /** Ответ по карточке. 409/404 — уже решена или снята: очередь перечитана, `stale`; прочее — «не отправилось». */
  async answerOwner(
    item: OwnerItem,
    body: OwnerAnswerBody,
  ): Promise<{ ok: true } | { ok: false; stale: boolean; message: string }> {
    try {
      await api.answerOwner(item.ref, body);
      await this.loadOwner();
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 404)) {
        await this.loadOwner();
        return { ok: false, stale: true, message: err.status === 409 ? 'Уже решено.' : 'Карточки уже нет.' };
      }
      if (err instanceof api.ApiError && err.status === 422) {
        return { ok: false, stale: false, message: err.message };
      }
      console.error('очередь владельца: ответ не прошёл', err);
      return { ok: false, stale: false, message: 'Не отправилось — попробуй ещё раз.' };
    } finally {
      void this.runSync();
    }
  }

  /** Ф65-1 (И3): ответ владельца на открытый вопрос агента по карточке — тот же путь, что в «Мой агент». */
  async answerAgentAsk(askId: number, text: string): Promise<{ ok: true } | { ok: false; message: string }> {
    try {
      await api.postOwnerTalk({ text, reply_to: askId });
      await this.loadOwner();
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 422 || err.status === 404)) {
        await this.loadOwner();
        return { ok: false, message: err.message };
      }
      console.error('очередь владельца: ответ агенту не ушёл', err);
      return { ok: false, message: 'Не отправилось — попробуй ещё раз.' };
    }
  }

  /** Поручение агенту (кнопка агента или свободный текст). 409/422 — текст ядра. */
  async startErrand(
    item: Pick<OwnerItem, 'ref'>,
    body: { action?: string; text?: string },
  ): Promise<{ ok: true } | { ok: false; message: string }> {
    try {
      await api.createErrand(item.ref, body);
      await this.loadOwner();
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 422 || err.status === 404)) {
        await this.loadOwner();
        return { ok: false, message: err.message };
      }
      console.error('очередь владельца: поручение не создано', err);
      return { ok: false, message: 'Не отправилось — попробуй ещё раз.' };
    }
  }

  async cancelErrand(id: number): Promise<{ ok: true } | { ok: false; message: string }> {
    try {
      await api.cancelErrand(id);
      await this.loadOwner();
      await this.loadHumanRequests(); // Ф58-3: «Забрать у агента» и из карточки заявки — кнопки владельца возвращаются
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 404)) {
        await this.loadOwner();
        return { ok: false, message: err.message };
      }
      console.error('очередь владельца: поручение не отозвано', err);
      return { ok: false, message: 'Не отправилось — попробуй ещё раз.' };
    }
  }

  /** Ф96-1: «Беру сам» / «Продолжай» — передача рук посреди поручения агента. */
  async errandHands(id: number, to: 'owner_channel' | 'agent', note?: string): Promise<{ ok: true } | { ok: false; message: string }> {
    try {
      await api.errandHands(id, to, note);
      await this.loadOwner();
      await this.loadHumanRequests();
      return { ok: true };
    } catch (err) {
      if (err instanceof api.ApiError && (err.status === 409 || err.status === 404 || err.status === 422)) {
        await this.loadOwner();
        return { ok: false, message: err.message };
      }
      console.error('очередь владельца: руки не переданы', err);
      return { ok: false, message: 'Не отправилось — попробуй ещё раз.' };
    }
  }

  /** Ждущие заявки агентов (`GET /agent-requests?status=pending`). Ручки может не быть (404) — молча без заявок. */
  async loadAgentRequests(): Promise<void> {
    const state = this.state.agentRequests;
    if (state.gone) return; // ручки нет в ядре — не стучимся, пока страница не перезагружена
    try {
      const body = await api.listAgentRequests({ status: 'pending' });
      for (const request of asList<AgentRequest>(body?.requests)) applyAgentRequest(state, request);
      state.error = null;
    } catch (err) {
      // ручки нет (404) или связи нет — заявок просто нет, ошибку в полосе не показываем
      if (err instanceof api.ApiError && err.status === 404) state.gone = true;
    }
  }

  /** «Перенести» / «Завести» (у напарника — с изменёнными полями). 409 — текст причины ядра возвращается карточке;
   * заявка остаётся ждущей. */
  async approveAgentRequest(
    request: AgentRequest,
    body: AgentRequestApproveBody = {},
  ): Promise<{ ok: true } | { ok: false; message: string }> {
    try {
      const res = await api.approveAgentRequest(request.id, body);
      if (res?.request) applyAgentRequest(this.state.agentRequests, res.request);
      if (res?.session) upsertSession(this.state.sessions, res.session);
      return { ok: true };
    } catch (err) {
      return { ok: false, message: this.agentRequestFailure(err) };
    } finally {
      void this.runSync();
    }
  }

  async declineAgentRequest(request: AgentRequest, note?: string): Promise<{ ok: true } | { ok: false; message: string }> {
    try {
      const res = await api.declineAgentRequest(request.id, note?.trim() || undefined);
      if (res?.request) applyAgentRequest(this.state.agentRequests, res.request);
      return { ok: true };
    } catch (err) {
      return { ok: false, message: this.agentRequestFailure(err) };
    } finally {
      void this.runSync();
    }
  }

  private agentRequestFailure(err: unknown): string {
    if (err instanceof api.ApiError && (err.status === 409 || err.status === 410 || err.status === 422) && err.message) {
      return err.message;
    }
    console.error('заявка агента: решение не прошло', err);
    return 'Не отправилось — попробуй ещё раз.';
  }

  // --- заявки на выход (Ф17-2b) ---------------------------------------------------------------------------------

  private applyLeaveChanges(leaves: LeaveRequest[] | undefined): void {
    for (const leave of Array.isArray(leaves) ? leaves : []) {
      try {
        applyLeave(this.state.leaves, leave);
      } catch (err) {
        console.error('sync: заявка на выход пропущена', err);
      }
    }
  }

  /** Открытые заявки и закрытые за 7 дней (`GET /leaves`): без `channelId` — все (главная, раз в 30 с), с ним —
   * заявки беседы (у темы — её форума) при открытии беседы (П12). Возвращает `channel_id` полученных заявок. */
  async loadLeaves(channelId?: number): Promise<number[]> {
    const leaves = this.state.leaves;
    try {
      const list = asList<LeaveRequest>(await api.listLeaves({ channelId, limit: 200 }));
      for (const leave of list) applyLeave(leaves, leave);
      leaves.error = null;
      return list.map((l) => l.channel_id);
    } catch {
      leaves.error = 'Заявки на выход не загрузились — повторю при следующем обновлении.';
      return [];
    }
  }

  /** «Выйти» / «Оставить» в карточке: 409 (уже решена, идёт вступление) — заявка перечитана. */
  async decideLeave(leave: LeaveRequest, decision: 'approve' | 'decline'): Promise<WorkActionResult> {
    try {
      const fresh = decision === 'approve' ? await api.approveLeave(leave.id) : await api.declineLeave(leave.id);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyLeave(this.state.leaves, fresh);
      return 'ok';
    } catch (err) {
      if (err instanceof api.ApiError && err.status === 409) {
        await this.loadLeaves();
        return 'stale';
      }
      console.error('заявка на выход: решение не прошло', err);
      return 'error';
    } finally {
      void this.runSync();
    }
  }

  // --- заявки на группы WhatsApp (Ф62-6) ------------------------------------------------------------------------

  private applyGroupOpChanges(ops: GroupOp[] | undefined): void {
    for (const op of Array.isArray(ops) ? ops : []) {
      try {
        applyGroupOp(this.state.groupOps, op);
      } catch (err) {
        console.error('sync: заявка на группу пропущена', err);
      }
    }
  }

  /** Открытые заявки и закрытые за 7 дней (`GET /group-ops`): без `channelId` — все, с ним — заявки беседы. */
  async loadGroupOps(channelId?: number): Promise<void> {
    const ops = this.state.groupOps;
    try {
      for (const op of asList<GroupOp>(await api.listGroupOps({ channelId, limit: 200 }))) applyGroupOp(ops, op);
      ops.error = null;
    } catch {
      ops.error = 'Заявки на группы не загрузились — повторю при следующем обновлении.';
    }
  }

  /** Перечитать одну заявку (`GET /group-ops/{id}`): окна ждут исход, не полагаясь на кадр потока. */
  async refreshGroupOp(opId: number): Promise<GroupOp> {
    const op = await api.getGroupOp(opId);
    if (op !== null && typeof op === 'object' && typeof op.id === 'number') applyGroupOp(this.state.groupOps, op);
    return op;
  }

  /** «Да» / «Отказать» в карточке: 409 (уже решена, уже выполняется) — заявка перечитана. */
  async decideGroupOp(op: GroupOp, decision: 'approve' | 'decline'): Promise<WorkActionResult> {
    try {
      const fresh = decision === 'approve' ? await api.approveGroupOp(op.id) : await api.declineGroupOp(op.id);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyGroupOp(this.state.groupOps, fresh);
      return 'ok';
    } catch (err) {
      if (err instanceof api.ApiError && err.status === 409) {
        await this.loadGroupOps();
        return 'stale';
      }
      console.error('заявка на группу: решение не прошло', err);
      return 'error';
    } finally {
      void this.runSync();
    }
  }

  /** Заявка владельца из интерфейса (создать группу, участники, ссылка): уходит сразу. Ошибка ядра — наверх, форме. */
  async submitGroupOp(input: GroupOpInput): Promise<GroupOp> {
    try {
      const op = await api.createGroupOp(input);
      if (op !== null && typeof op === 'object' && typeof op.id === 'number') applyGroupOp(this.state.groupOps, op);
      return op;
    } finally {
      void this.runSync();
    }
  }

  /** Кнопка шапки беседы. Ошибка ядра уходит вызывающему (строка рядом с кнопкой); заявка кладётся в стор. */
  async leaveChannel(channelId: number): Promise<void> {
    try {
      const fresh = await api.leaveChannel(channelId);
      if (fresh !== null && typeof fresh === 'object' && typeof fresh.id === 'number') applyLeave(this.state.leaves, fresh);
    } finally {
      void this.runSync();
    }
  }

  // --- отправка/действия над outbox --------------------------------------------------------------------------

  async send(
    channelId: number,
    text: string,
    idempotencyKey?: string,
    opts: { thread?: string; replyTo?: string; files?: string[] } = {},
  ): Promise<void> {
    await api.sendMessage({
      channelId,
      text,
      idempotencyKey,
      thread: opts.thread,
      replyTo: opts.replyTo,
      files: opts.files,
    });
    void this.runSync();
  }

  /** Ф12-5 C2: «Новое сообщение» — беседа-заготовка из ответа ядра сразу в стор (не ждать `/sync`), вкладка —
   * проект беседы, беседа открыта, фокус — в поле ввода. Ошибка ядра (422/429) — наверх, форме. Ф14-2: «по
   * правилам» (`projectId: null`) без подходящего правила — вкладка «Неразобранное». */
  async createNewChannel(input: api.ChannelNewInput): Promise<Channel> {
    const { channel } = await api.createChannel(input);
    this.state.sync.channelsById[channel.id] = channel;
    const projectId = channel.project_id ?? input.projectId ?? UNSORTED_PROJECT_ID;
    this.state.composerFocusChannelId = channel.id;
    // Беседа уже создана: сбой догрузки списка или ленты — не ошибка формы, их догонит `/sync`.
    if (this.state.currentProjectId !== projectId || this.state.homeOpen) {
      await this.openProject(projectId, { restore: false }).catch(() => undefined);
    }
    await this.openChannel(channel.id).catch(() => undefined);
    return channel;
  }

  /** Ф46-1: «Написать автору» — заготовка лички автору сообщения группы Telegram (или уже существующая личка),
   * затем беседа открывается: окном поверх на «Главной» компьютера, иначе обычным переходом. Ошибка ядра — наверх. */
  async writeToAuthor(message: { id: number; channel_id: number }): Promise<Channel> {
    const source = this.state.sync.channelsById[message.channel_id];
    const accountId = await api.getChannelAccountId(message.channel_id);
    const projectId = source?.project_id ?? null;
    const { channel } = await api.createChannel({
      accountId,
      projectId,
      to: { kind: 'tg_msg_author', message_id: message.id },
    });
    this.state.sync.channelsById[channel.id] = channel;
    this.state.composerFocusChannelId = channel.id;
    if (!isPhone()) {
      await this.openChannelOverlay(channel.id);
      return channel;
    }
    await this.openChannelAt(channel.project_id ?? projectId ?? UNSORTED_PROJECT_ID, channel.id).catch(
      () => undefined,
    );
    return channel;
  }

  /** Ф10-4 A2: файл из поля ввода — сразу в ядро (к беседе); отправка потом ссылается на `upload_id`. */
  async uploadFile(
    channelId: number,
    file: Blob,
    name: string,
    opts: { voice?: boolean } = {},
  ): Promise<UploadResult> {
    return api.uploadFile(channelId, file, name, opts);
  }

  async cancelOutbox(outboxId: number): Promise<OutboxStatus> {
    const result = await api.cancelOutbox(outboxId);
    this.dropDraft(outboxId);
    void this.runSync();
    return result.status as OutboxStatus;
  }

  async approveOutbox(outboxId: number, text?: string): Promise<void> {
    await api.approveOutbox(outboxId, text);
    this.dropDraft(outboxId);
    void this.runSync();
  }

  /** Ф30-1 C: «Отправить сейчас» — время `send_at` черновика прошло (409 `send_at_passed`), уходит обычным
   * порядком. Текст — как у «Отправить» (правленый, если правили). */
  async approveOutboxNow(outboxId: number, text?: string): Promise<void> {
    await api.approveOutbox(outboxId, text, true);
    this.dropDraft(outboxId);
    void this.runSync();
  }

  /** 24.10: «Отправить сейчас» у подтверждённой строки по графику или к сроку — график и срок снимаются,
   * строка уходит из «По графику» сразу (следующее обновление подтвердит). Ошибка (409 с причиной) — наверх. */
  async sendOutboxNow(outboxId: number): Promise<void> {
    await api.sendOutboxNow(outboxId);
    this.dropDraft(outboxId);
    void this.runSync();
  }

  /** Черновик подтверждён или отклонён — сразу убрать из списков (следующее обновление подтвердит). */
  private dropDraft(outboxId: number): void {
    this.state.home.drafts = this.state.home.drafts.filter((d) => d.id !== outboxId);
    this.state.home.scheduled = (this.state.home.scheduled ?? []).filter((d) => d.id !== outboxId);
    this.state.draftSelection = this.state.draftSelection.filter((id) => id !== outboxId);
  }

  async resolveUnknown(outboxId: number, decision: ResolveUnknownDecision): Promise<void> {
    await api.resolveUnknownOutbox(outboxId, decision);
    void this.runSync();
  }

  async markRead(channelId: number, upToMessageId: number): Promise<void> {
    try {
      await api.markChannelRead(channelId, upToMessageId);
    } catch (err) {
      // Ф40-4: беседа-заготовка после первого сообщения склеилась с настоящей (или беседа скрыта) — ядро её
      // больше не знает, а лента ещё успела отметить прочтение: не ошибка, `/sync` уберёт беседу сам.
      if (err instanceof api.ApiError && err.status === 404) return;
      throw err;
    }
  }

  /** Ф8-5 A2.3: «Загрузить»/«Повторить» — задание в очередь ядра; обновлённый `media` сразу в стор (П8),
   * дальше статусы приходят обычным `/sync` (каждая смена статуса — новый `seq` сообщения). */
  async fetchMedia(messageId: number, idx: number): Promise<void> {
    const result = await api.fetchMedia(messageId, idx);
    applyMediaUpdate(this.state.sync, messageId, result.media);
    void this.runSync();
  }

  /** Ф14-6: расшифровать голосовое (или заново) — строка `transcript` сразу `queued`, дальше — `/sync`. */
  async transcribeMedia(mediaId: number): Promise<void> {
    const result = await api.transcribeMedia(mediaId);
    applyMediaUpdate(this.state.sync, result.message_id, result.media);
    void this.runSync();
  }

  // --- Ф8-4: удалить у себя в хабе (у источника ничего не удаляется) ------------------------------------

  /** B4 + П11: одно сообщение или все части альбома — `DELETE` на каждое по очереди; строка убирается из
   * стора сразу после ответа, не дожидаясь `/sync`. Ошибка (409 — своё исходящее ещё отправляется) — наружу,
   * уже удалённые части остаются удалёнными. */
  async hideMessages(messageIds: number[]): Promise<void> {
    try {
      for (const messageId of messageIds) {
        await api.hideMessage(messageId);
        removeMessage(this.state.sync, messageId);
      }
    } finally {
      void this.runSync();
    }
  }

  // --- Ф14-8: правка и «удалить у всех» своих сообщений (раздел D) --------------------------------------

  /** Правка — `op` из ответа сразу в стор («сохраняется…»); `unchanged` — ничего не изменилось. */
  async editMessage(messageId: number, text: string): Promise<'queued' | 'unchanged'> {
    const result = await api.editMessage(messageId, text);
    applyMessageOp(this.state.sync, messageId, result.op);
    void this.runSync();
    return result.status;
  }

  /** «Удалить у всех» — одно сообщение или части альбома одним запросом; `op` каждой части сразу в стор. */
  async revokeMessages(messageIds: number[]): Promise<void> {
    const result = await api.revokeMessages(messageIds);
    for (const [id, op] of Object.entries(result.ops ?? {})) applyMessageOp(this.state.sync, Number(id), op);
    void this.runSync();
  }

  /** «Отменить»/«Скрыть» — снять строку операции (по очереди у частей альбома); ошибка (409 `in_flight`) —
   * наружу, уже снятые остаются снятыми. */
  async cancelMessageOps(messageIds: number[]): Promise<void> {
    try {
      for (const messageId of messageIds) {
        await api.cancelMessageOp(messageId);
        applyMessageOp(this.state.sync, messageId, null);
      }
    } finally {
      void this.runSync();
    }
  }

  /** B4: беседа целиком — пропадает из списка, открытая закрывается. */
  async hideChannel(channelId: number): Promise<void> {
    await api.hideChannel(channelId);
    this.onChannelGone(channelId);
    removeChannel(this.state.sync, channelId);
    void this.runSync();
  }

  private onChannelGone(channelId: number): void {
    if (this.state.openThread?.channelId === channelId) this.closeThread();
    if (this.state.currentChannelId === channelId) {
      this.state.currentChannelId = null;
      this.presence?.update();
    }
  }

  /** Ф10-3 П1: одна операция реакции (`on` — поставить, иначе снять). Правду о реакциях пришлёт источник
   * событием `reaction` — через `/sync`/SSE, как любое изменение сообщения. */
  async setReaction(messageId: number, emoji: string, on: boolean): Promise<'queued' | 'off'> {
    return (await api.setReaction(messageId, emoji, on)).status;
  }

  /** C4: прежние версии правленого сообщения (от старой к новой). */
  async loadEdits(messageId: number): Promise<MessageEdit[]> {
    return (await api.listMessageEdits(messageId)).edits;
  }

  async muteChannel(channelId: number, muted: boolean): Promise<void> {
    await api.patchChannel(channelId, { muted });
    void this.runSync();
  }

  /** Ф8-3 D1: ручной режим внимания беседы; `null` — «Авто» (по политике проекта). */
  async setChannelAttention(channelId: number, attention: AttentionMode | null): Promise<void> {
    await api.patchChannel(channelId, { attention });
    void this.runSync();
  }

  /** Ф34-1: стадия беседы. `note` не передан — заметку решает ядро (смена стадии стирает, то же написание — оставляет);
   * `stage = null` снимает стадию вместе с заметкой. Ошибка 422 — пробрасывается с текстом `detail`. */
  async setChannelStage(channelId: number, stage: string | null, note?: string | null): Promise<void> {
    const fields: { stage: string | null; stage_note?: string | null } = { stage };
    if (note !== undefined) fields.stage_note = note;
    await api.patchChannel(channelId, fields);
    await this.runSync();
  }

  /** Ф34-1: правка только заметки при текущей стадии. */
  async setChannelStageNote(channelId: number, note: string | null): Promise<void> {
    await api.patchChannel(channelId, { stage_note: note });
    await this.runSync();
  }

  /** Ф14-1 C1: проект беседы из шапки — проект / «Неразобранное» (`null`) вручную или «По правилам». */
  async setChannelProject(channelId: number, target: MoveTarget): Promise<void> {
    await api.patchChannel(channelId, patchFieldsOf(target));
    this.refreshAfterMapping();
  }

  /** Ф14-1 C2: перенос пачкой; ошибка (404/422) — пробрасывается вызывающему, ничего не записано. */
  async moveChannels(channelIds: number[], target: MoveTarget): Promise<number> {
    const { moved } = await api.moveChannels(channelIds, target);
    this.refreshAfterMapping();
    return moved;
  }

  /** После переноса/правила — изменения каналов через `/sync`, счётчики вкладок — `loadProjects`. */
  refreshAfterMapping(): void {
    void this.runSync();
    this.loadProjects().catch(() => undefined);
  }
}

// re-export для псевдо-проектов, чтобы компонентам не тянуть их отдельно из api/types в этом файле
export { ALL_PROJECTS_ID, UNSORTED_PROJECT_ID };

/** Минимум от `Window`, нужный для чтения/очистки кода входа — сужено намеренно (а не просто `Window`), чтобы
 * тест 10 мог подставить лёгкий фейк вместо настоящего `window` (без реального jsdom-навигейшна и побочных
 * эффектов остального `bootstrap()`, вроде подключения `EventSource`). Настоящий `window` эту форму, конечно,
 * тоже удовлетворяет — используется как значение по умолчанию без каста. */
export interface LoginWindowLike {
  location: { hash: string; pathname: string; search: string };
  history: { replaceState: (data: unknown, unused: string, url?: string | URL | null) => void };
}

/** Код входа — из `location.hash` (`#code=...`), СРАЗУ вычищенный `history.replaceState` (спека, «Вход»:
 * «прочитал hash → history.replaceState → POST», строго в этом порядке, ДО сетевого запроса). Отдельная
 * экспортируемая функция — тест 10 проверяет порядок и очистку без похода в сеть. */
export function readAndClearLoginCode(win: LoginWindowLike = window): string | null {
  const hash = win.location.hash;
  if (!hash || hash.length < 2) return null;
  const params = new URLSearchParams(hash.slice(1));
  const code = params.get('code');
  if (code !== null) {
    win.history.replaceState(null, '', win.location.pathname + win.location.search);
  }
  return code;
}

export interface HashLoginResult {
  attempted: boolean;
  ok: boolean;
}

/** Оркестрация «код из хеша -> вход», выделенная отдельно от `Store.bootstrap()` ради теста 10: здесь и
 * только здесь важен порядок (очистка хеша ДО сетевого запроса) — без побочных эффектов остального стора. */
export async function loginFromLocationHash(
  win: LoginWindowLike,
  loginFn: (code: string) => Promise<unknown>,
): Promise<HashLoginResult> {
  const code = readAndClearLoginCode(win);
  if (code === null) return { attempted: false, ok: false };
  try {
    await loginFn(code);
    return { attempted: true, ok: true };
  } catch {
    return { attempted: true, ok: false };
  }
}

export function createStore(deps?: StoreDeps): Store {
  return new Store(deps);
}
