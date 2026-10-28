// Ф10-7 (`docs/architecture.md`): вход с телефона и панель «Доступ и входы» —
// чистые функции без Vue (адрес страницы, тексты ошибок входа паролем, подписи строк входов).
import { ApiError } from '../api/client';
import type { RemoteStatus, SessionVia } from '../api/types';

/** Продление сессии (`POST /auth/renew`): после входа, раз в 6 ч и при возврате на вкладку, если прошло > 6 ч. */
export const RENEW_INTERVAL_MS = 6 * 60 * 60 * 1000;

/** Пароль доступа с телефона — 10–200 символов (решение 3; ядро на другое отвечает 422). */
export const PASSWORD_MIN = 10;
export const PASSWORD_MAX = 200;

const LOCAL_HOSTNAMES = new Set(['127.0.0.1', 'localhost']);

/** Страница открыта не на ноутбуке (адрес не `127.0.0.1`/`localhost`) — вход паролем, а не `hub ui`. */
export function isRemoteHostname(hostname: string): boolean {
  return !LOCAL_HOSTNAMES.has(hostname);
}

export const LOGIN_TEXT_INVALID = 'Неверный пароль';
export const LOGIN_TEXT_UNAVAILABLE = 'Доступ с телефона выключен или ноутбук недоступен';

/** Текст ошибки входа паролем: 401 — неверный, 429 — «через N мин», прочее (403/421/сеть/5xx) — выключено. */
export function passwordLoginError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return LOGIN_TEXT_INVALID;
    if (err.status === 429) return `Слишком много попыток — попробуй через ${retryMinutes(err.body)} мин`;
  }
  return LOGIN_TEXT_UNAVAILABLE;
}

function retryMinutes(body: unknown): number {
  const raw =
    body !== null && typeof body === 'object' && 'retry_after_s' in body
      ? Number((body as { retry_after_s: unknown }).retry_after_s)
      : NaN;
  if (!Number.isFinite(raw) || raw <= 0) return 1;
  return Math.max(1, Math.ceil(raw / 60));
}

/** Проверка нового пароля в панели (до запроса): `null` — годится. */
export function newPasswordProblem(password: string, repeat: string): string | null {
  if (password.length < PASSWORD_MIN) return `Пароль — не короче ${PASSWORD_MIN} символов`;
  if (password.length > PASSWORD_MAX) return `Пароль — не длиннее ${PASSWORD_MAX} символов`;
  if (password !== repeat) return 'Пароли не совпадают';
  return null;
}

/** Адреса из поля панели: через запятую, пробел или с новой строки; нижний регистр, без пустых. */
export function parseHosts(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((h) => h.trim().toLowerCase())
    .filter((h) => h !== '');
}

export const CONFIRM_REVOKE_OTHERS = 'Выйти на всех устройствах, кроме этого?';
export const CONFIRM_REVOKE_ALL = 'Выйти на всех устройствах, включая этот?';

export function viaLabel(via: SessionVia): string {
  if (via === 'local') return 'ноутбук';
  if (via === 'remote') return 'телефон';
  return 'неизвестно';
}

const OS_RULES: [RegExp, string][] = [
  [/iPhone/, 'iPhone'],
  [/iPad/, 'iPad'],
  [/Android/, 'Android'],
  [/Windows/, 'Windows'],
  [/CrOS/, 'ChromeOS'],
  [/Macintosh|Mac OS X/, 'macOS'],
  [/Linux|X11/, 'Linux'],
];

// Порядок важен: у Edge, Opera, Яндекса и Samsung в строке тоже есть `Chrome/` и `Safari/`, у Chrome — `Safari/`.
const BROWSER_RULES: [RegExp, string][] = [
  [/YaBrowser\//, 'Яндекс'],
  [/SamsungBrowser\//, 'Samsung Internet'],
  [/Edg(e|A|iOS)?\//, 'Edge'],
  [/OPR\/|Opera/, 'Opera'],
  [/Firefox\/|FxiOS\//, 'Firefox'],
  [/CriOS\//, 'Chrome'],
  [/Chromium\//, 'Chromium'],
  [/Chrome\//, 'Chrome'],
  [/Version\/[\d.]+.*Safari\//, 'Safari'],
];

/** Устройство из `User-Agent` коротко: «Android · Chrome», «iPhone · Safari», «Linux · Chromium». */
export function deviceLabel(userAgent: string | null | undefined): string {
  const ua = userAgent ?? '';
  const os = OS_RULES.find(([re]) => re.test(ua))?.[1] ?? null;
  const browser = BROWSER_RULES.find(([re]) => re.test(ua))?.[1] ?? null;
  if (os !== null && browser !== null) return `${os} · ${browser}`;
  return os ?? browser ?? 'неизвестное устройство';
}

/** «25.09 14:03» — дата и время входа/последнего использования; `null` — «до 25.09» (строки до миграции 0019). */
export function sessionTime(ts: number | null): string {
  if (ts === null) return 'до 25.09';
  return new Date(ts).toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// --- Ф14-9 (`docs/architecture.md`: https удалённого слушателя в панели «Доступ» ---

/** Меньше стольких дней до конца сертификата — предупреждение (certbot продлевает за 30). */
export const TLS_WARN_DAYS = 14;
const DAY_MS = 24 * 60 * 60 * 1000;

export const TLS_TEXT_HTTP =
  'http — без https на телефоне не работают голосовые и вставка картинок (см. README)';

export type TlsLevel = 'ok' | 'warn' | 'error';

/** «27.12» — день и месяц по местному времени. */
function dayMonth(ms: number): string {
  const d = new Date(ms);
  return `${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}`;
}

/** Строка про https и её уровень: ошибка пары, срок (< 14 дней — предупреждение, истёк — ошибка), http. */
export function tlsLine(status: RemoteStatus, nowMs: number = Date.now()): { text: string; level: TlsLevel } {
  const tls = status.tls;
  if (tls === null || tls === undefined) return { text: TLS_TEXT_HTTP, level: 'warn' };
  if (tls.error !== null) return { text: `https: ошибка — ${tls.error}; вход с телефона не поднят`, level: 'error' };
  if (tls.not_after_ms === null) return { text: 'https', level: 'ok' };
  const until = dayMonth(tls.not_after_ms);
  if (tls.not_after_ms <= nowMs) return { text: `https: сертификат истёк ${until} — продление не идёт`, level: 'error' };
  const days = Math.floor((tls.not_after_ms - nowMs) / DAY_MS);
  if (days < TLS_WARN_DAYS) {
    return { text: `https, сертификат до ${until} — осталось ${days} дн., продление не идёт?`, level: 'warn' };
  }
  return { text: `https, сертификат до ${until}`, level: 'ok' };
}

/** Имя сертификата подходит к имени хоста (`*.домен` — ровно одна метка слева). */
function nameMatches(pattern: string, hostname: string): boolean {
  const p = pattern.toLowerCase();
  if (p.startsWith('*.')) {
    const rest = hostname.split('.').slice(1).join('.');
    return hostname.includes('.') && rest === p.slice(2);
  }
  return p === hostname;
}

/** Адреса из «hosts», которых нет в сертификате (при рабочем https). */
export function hostsNotInCert(status: RemoteStatus): string[] {
  const tls = status.tls;
  if (!tls || tls.error !== null) return [];
  return status.hosts.filter((host) => {
    const hostname = host.replace(/:\d+$/, '').toLowerCase();
    return !tls.names.some((name) => nameMatches(name, hostname));
  });
}

/** Адрес для телефона — со схемой, которую реально отдаёт удалённый слушатель. */
export function phoneUrl(status: RemoteStatus, host: string): string {
  const https = status.tls !== null && status.tls !== undefined && status.tls.error === null;
  return `${https ? 'https' : 'http'}://${host}`;
}
