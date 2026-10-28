// Ф10-7 B (`docs/architecture.md`): UI без безопасного контекста (ключи без `crypto.randomUUID`,
// копирование без `navigator.clipboard`), вход паролем (П7: 401 не уводит на «сессия истекла»), продление
// сессии, панель «Доступ и входы». Экран входа с телефона (не-loopback адрес страницы) — тест 41.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

import * as api from '../src/api/client';
import type { RemoteStatus, UiSession } from '../src/api/types';
import AccessPanel from '../src/components/AccessPanel.vue';
import Composer from '../src/components/Composer.vue';
import LoginView from '../src/components/LoginView.vue';
import ModelText from '../src/components/ModelText.vue';
import SessionExpiredView from '../src/components/SessionExpiredView.vue';
import { store } from '../src/store/instance';
import { PresenceTracker } from '../src/store/presence';
import { createStore } from '../src/store/store';
import type { EventSourceLike } from '../src/store/stream';
import { copyText } from '../src/util/copy';
import { newId, uuidV4FromRandom } from '../src/util/ids';
import {
  CONFIRM_REVOKE_ALL,
  deviceLabel,
  isRemoteHostname,
  LOGIN_TEXT_INVALID,
  LOGIN_TEXT_UNAVAILABLE,
  newPasswordProblem,
  parseHosts,
  passwordLoginError,
  RENEW_INTERVAL_MS,
  viaLabel,
} from '../src/util/remote';

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** `crypto` без `randomUUID` — как в браузере телефона по http (не безопасный контекст). */
function stubInsecureCrypto(): void {
  const real = globalThis.crypto;
  vi.stubGlobal('crypto', { getRandomValues: <T extends ArrayBufferView>(a: T): T => real.getRandomValues(a as never) });
}

beforeEach(() => {
  api.setCsrf(null);
  api.setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

// --- 1. ключи без crypto.randomUUID --------------------------------------------------------------------------

describe('Ф10-7 B1: newId без безопасного контекста', () => {
  it('uuidV4FromRandom — версия 4 и вариант 10xx при любых байтах', () => {
    expect(uuidV4FromRandom(new Uint8Array(16).fill(0xff))).toMatch(UUID_V4);
    expect(uuidV4FromRandom(new Uint8Array(16))).toBe('00000000-0000-4000-8000-000000000000');
  });

  it('без crypto.randomUUID — v4 из getRandomValues, разные', () => {
    stubInsecureCrypto();
    expect(typeof globalThis.crypto.randomUUID).toBe('undefined');
    const a = newId();
    const b = newId();
    expect(a).toMatch(UUID_V4);
    expect(b).toMatch(UUID_V4);
    expect(a).not.toBe(b);
  });

  it('Composer: без crypto.randomUUID отправка идёт, ключ — v4', async () => {
    stubInsecureCrypto();
    store.state.sync.channelsById = {
      1: {
        id: 1,
        ext_id: 'c1',
        kind: 'dm',
        title: 'Канал',
        tier: null,
        muted: false,
        archived: false,
        map_reason: null,
        last_msg_ts: 1000,
        my_msg_count: 0,
        first_seen_ts: null,
        unread: 0,
        project_id: 1,
        source: 'telegram',
      },
    };
    const sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined);
    const w = mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
    await w.get('textarea').setValue('привет с телефона');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]![1]).toBe('привет с телефона');
    expect(sendSpy.mock.calls[0]![2]).toMatch(UUID_V4);
    expect(w.find('.composer-error').exists()).toBe(false);
    w.unmount();
    store.state.sync.channelsById = {};
  });

  it('api.sendMessage без ключа и без crypto.randomUUID — idempotency_key v4', async () => {
    stubInsecureCrypto();
    const fetchMock = vi.fn(async () => jsonResponse({ outbox_id: 1, status: 'queued' }));
    vi.stubGlobal('fetch', fetchMock);
    await api.sendMessage({ channelId: 1, text: 'x' });
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(JSON.parse(String(init.body)).idempotency_key).toMatch(UUID_V4);
  });

  it('присутствие: client_id без crypto.randomUUID — v4', () => {
    stubInsecureCrypto();
    const tracker = new PresenceTracker({ post: async () => {}, target: () => null, isActive: () => false });
    expect(tracker.clientId).toMatch(UUID_V4);
  });

  it('сторож: crypto.randomUUID в ui/src — только в util/ids.ts', () => {
    const src = join(__dirname, '..', 'src');
    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) walk(full);
        else if (/\.(ts|vue)$/.test(entry) && /randomUUID\s*\(/.test(readFileSync(full, 'utf8'))) offenders.push(full);
      }
    };
    walk(src);
    expect(offenders.map((f) => f.slice(src.length + 1))).toEqual(['util/ids.ts']);
  });
});

// --- 1. копирование без navigator.clipboard ------------------------------------------------------------------

describe('Ф10-7 B1: copyText без безопасного контекста', () => {
  function stubExecCommand(result: boolean) {
    const copied: string[] = [];
    const exec = vi.fn((cmd: string) => {
      const area = document.querySelector<HTMLTextAreaElement>('textarea.copy-buffer');
      copied.push(`${cmd}:${area?.value.slice(area.selectionStart, area.selectionEnd) ?? '<нет поля>'}`);
      return result;
    });
    Object.defineProperty(document, 'execCommand', { value: exec, configurable: true });
    return { exec, copied };
  }

  afterEach(() => {
    delete (document as unknown as { execCommand?: unknown }).execCommand;
    delete (navigator as unknown as { clipboard?: unknown }).clipboard;
  });

  it('нет navigator.clipboard — скрытое поле и execCommand("copy"); поле убрано', async () => {
    vi.stubGlobal('isSecureContext', false);
    const { exec, copied } = stubExecCommand(true);
    expect(await copyText('uv run pytest')).toBe(true);
    expect(exec).toHaveBeenCalledWith('copy');
    expect(copied).toEqual(['copy:uv run pytest']);
    expect(document.querySelector('textarea.copy-buffer')).toBeNull();
  });

  it('http: clipboard есть, но контекст не безопасный — всё равно execCommand', async () => {
    vi.stubGlobal('isSecureContext', false);
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { exec } = stubExecCommand(true);
    expect(await copyText('a')).toBe(true);
    expect(exec).toHaveBeenCalledTimes(1);
    expect(writeText).not.toHaveBeenCalled();
  });

  it('безопасный контекст — navigator.clipboard, без execCommand', async () => {
    vi.stubGlobal('isSecureContext', true);
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { exec } = stubExecCommand(true);
    expect(await copyText('b')).toBe(true);
    expect(writeText).toHaveBeenCalledWith('b');
    expect(exec).not.toHaveBeenCalled();
  });

  it('execCommand отказал — false', async () => {
    vi.stubGlobal('isSecureContext', false);
    stubExecCommand(false);
    expect(await copyText('c')).toBe(false);
  });

  it('ModelText: «копировать» у кода без clipboard — через execCommand, «скопировано»', async () => {
    vi.stubGlobal('isSecureContext', false);
    const { copied } = stubExecCommand(true);
    const w = mount(ModelText, { props: { text: '```\nls -la\n```' }, attachTo: document.body });
    const button = w.get('button.md-copy').element as HTMLButtonElement;
    button.click();
    await flushPromises();
    expect(copied).toEqual(['copy:ls -la\n']);
    expect(button.textContent).toBe('скопировано');
    w.unmount();
  });
});

// --- API-клиент --------------------------------------------------------------------------------------------

describe('Ф10-7 П7: вход паролем в API-клиенте', () => {
  it('401 на /auth/password — общий обработчик не зовётся, ApiError', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'invalid password' }, 401)));
    await expect(api.loginWithPassword('wrong-password')).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).not.toHaveBeenCalled();
  });

  it('успех — POST /api/v1/auth/password {password}, CSRF в памяти модуля', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ csrf: 'csrf-remote', expires_ts: 1 }));
    vi.stubGlobal('fetch', fetchMock);
    await api.loginWithPassword('correct horse');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/v1/auth/password');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ password: 'correct horse' });
    expect(api.getCsrf()).toBe('csrf-remote');
  });

  it('ручки сессий и доступа — пути, методы, тела по контракту', async () => {
    const calls: [string, string, unknown][] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push([init.method ?? 'GET', url, init.body === undefined ? undefined : JSON.parse(String(init.body))]);
        return jsonResponse({});
      }),
    );
    await api.renewSession();
    await api.listSessions();
    await api.revokeSession(7);
    await api.revokeAllSessions(true);
    await api.getRemote();
    await api.putRemote({ enabled: true });
    await api.putRemote({ hosts: ['hub.example.com:8787'] });
    await api.setRemotePassword('0123456789');
    expect(calls).toEqual([
      ['POST', '/api/v1/auth/renew', {}],
      ['GET', '/api/v1/auth/sessions', undefined],
      ['POST', '/api/v1/auth/sessions/7/revoke', {}],
      ['POST', '/api/v1/auth/sessions/revoke-all', { keep_current: true }],
      ['GET', '/api/v1/remote', undefined],
      ['PUT', '/api/v1/remote', { enabled: true }],
      ['PUT', '/api/v1/remote', { hosts: ['hub.example.com:8787'] }],
      ['PUT', '/api/v1/remote/password', { password: '0123456789' }],
    ]);
  });
});

// --- чистые функции ----------------------------------------------------------------------------------------

describe('Ф10-7 B2/B4: тексты и подписи', () => {
  it('адрес страницы: локальный только 127.0.0.1 и localhost', () => {
    expect(isRemoteHostname('127.0.0.1')).toBe(false);
    expect(isRemoteHostname('localhost')).toBe(false);
    expect(isRemoteHostname('hub.example.com')).toBe(true);
    expect(isRemoteHostname('192.168.1.5')).toBe(true);
  });

  it('ошибки входа паролем: 401, 429 с минутами, 403/421/сеть', () => {
    expect(passwordLoginError(new api.ApiError(401, 'invalid password', null))).toBe(LOGIN_TEXT_INVALID);
    expect(
      passwordLoginError(new api.ApiError(429, 'too many attempts', { detail: 'x', retry_after_s: 90 })),
    ).toBe('Слишком много попыток — попробуй через 2 мин');
    expect(passwordLoginError(new api.ApiError(429, 'x', { retry_after_s: 3600 }))).toBe(
      'Слишком много попыток — попробуй через 60 мин',
    );
    expect(passwordLoginError(new api.ApiError(429, 'x', { retry_after_s: 5 }))).toBe(
      'Слишком много попыток — попробуй через 1 мин',
    );
    expect(passwordLoginError(new api.ApiError(403, 'forbidden', null))).toBe(LOGIN_TEXT_UNAVAILABLE);
    expect(passwordLoginError(new api.ApiError(421, 'misdirected', null))).toBe(LOGIN_TEXT_UNAVAILABLE);
    expect(passwordLoginError(new api.NetworkError(new Error('offline')))).toBe(LOGIN_TEXT_UNAVAILABLE);
  });

  it('устройство из User-Agent', () => {
    expect(
      deviceLabel(
        'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36',
      ),
    ).toBe('Android · Chrome');
    expect(
      deviceLabel(
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1',
      ),
    ).toBe('iPhone · Safari');
    expect(
      deviceLabel(
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/128.0.0.0 Chrome/128.0.0.0 Safari/537.36',
      ),
    ).toBe('Linux · Chromium');
    expect(
      deviceLabel(
        'Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36',
      ),
    ).toBe('Android · Samsung Internet');
    expect(deviceLabel('Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0')).toBe(
      'Linux · Firefox',
    );
    expect(deviceLabel(null)).toBe('неизвестное устройство');
    expect(deviceLabel('curl/8.5')).toBe('неизвестное устройство');
  });

  it('откуда вход, адреса, новый пароль', () => {
    expect([viaLabel('local'), viaLabel('remote'), viaLabel(null)]).toEqual(['ноутбук', 'телефон', 'неизвестно']);
    expect(parseHosts(' Hub.Example.com:8787,  hub2.example.com:8787\n')).toEqual([
      'hub.example.com:8787',
      'hub2.example.com:8787',
    ]);
    expect(newPasswordProblem('short', 'short')).toMatch(/не короче 10/);
    expect(newPasswordProblem('0123456789', '0123456788')).toBe('Пароли не совпадают');
    expect(newPasswordProblem('x'.repeat(201), 'x'.repeat(201))).toMatch(/не длиннее 200/);
    expect(newPasswordProblem('0123456789', '0123456789')).toBeNull();
  });
});

// --- стор: вход паролем, продление, выход ------------------------------------------------------------------

class FakeEventSource implements EventSourceLike {
  readyState = 1;
  addEventListener(): void {}
  close(): void {}
}

function storeRouter(opts: { me?: Record<string, unknown>; password?: Response } = {}) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    calls.push(`${init?.method ?? 'GET'} ${url.pathname}`);
    switch (url.pathname) {
      case '/api/v1/auth/me':
        return jsonResponse({
          token_id: 1,
          kind: 'ui',
          scopes: ['read', 'send', 'admin'],
          project_ids: null,
          csrf: 'c',
          ...opts.me,
        });
      case '/api/v1/auth/password':
        return opts.password ?? jsonResponse({ csrf: 'c2', expires_ts: 1 });
      case '/api/v1/auth/renew':
        return jsonResponse({ expires_ts: 2 });
      case '/api/v1/status':
        return jsonResponse({ accounts: [], dead_letter: {}, outbox_pending: 0, outbox_unknown: 0, max_seq: 5, now: 0 });
      case '/api/v1/projects':
        return jsonResponse([]);
      case '/api/v1/sync':
        return jsonResponse({ messages: [], channels: [], max_seq: 5, more: false });
      case '/api/v1/auth/sessions/3/revoke':
      case '/api/v1/auth/sessions/4/revoke':
        return jsonResponse({ ok: true });
      case '/api/v1/auth/sessions/revoke-all':
        return jsonResponse({ revoked: 2 });
      default:
        return jsonResponse([]);
    }
  });
  const renews = () => calls.filter((c) => c === 'POST /api/v1/auth/renew').length;
  return { fetchMock, calls, renews };
}

const NO_CODE = { location: { hash: '', pathname: '/', search: '' }, history: { replaceState: () => {} } };

/** Остановить фон стора (таймеры, слушатели `visibilitychange`), чтобы он не отвечал в следующих тестах. */
function stop(s: ReturnType<typeof createStore>): void {
  (s as unknown as { stopBackground(): void }).stopBackground();
}

async function flushAll(): Promise<void> {
  for (let i = 0; i < 5; i++) await flushPromises();
}

describe('Ф10-7 B3: продление сессии', () => {
  it('после bootstrap — сразу, дальше раз в 6 ч; возврат на вкладку — только если прошло > 6 ч', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
    const { fetchMock, renews } = storeRouter({ me: { remote: true } });
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore({ createEventSource: () => new FakeEventSource(), presenceActive: () => false });
    await s.bootstrap(NO_CODE);
    await flushAll();
    expect(s.state.screen).toBe('app');
    expect(s.state.remoteSession).toBe(true);
    expect(renews()).toBe(1);

    // вкладка вернулась через час — не продлеваем
    vi.setSystemTime(Date.now() + 60 * 60 * 1000);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushAll();
    expect(renews()).toBe(1);

    // таймер 6 ч (часы таймеров `setSystemTime` не двигает)
    await vi.advanceTimersByTimeAsync(RENEW_INTERVAL_MS - 1000);
    await flushAll();
    expect(renews()).toBe(1);
    await vi.advanceTimersByTimeAsync(1000);
    await flushAll();
    expect(renews()).toBe(2);

    // вкладка спала (таймеры не шли) 7 ч — при возврате продлеваем
    vi.setSystemTime(Date.now() + 7 * 60 * 60 * 1000);
    document.dispatchEvent(new Event('visibilitychange'));
    await flushAll();
    expect(renews()).toBe(3);

    stop(s);
  });

  it('вход паролем — сессия удалённая и продлевается; 401 — «Неверный пароль», экран не «сессия истекла»', async () => {
    const bad = storeRouter({ password: jsonResponse({ detail: 'invalid password' }, 401) });
    vi.stubGlobal('fetch', bad.fetchMock);
    const s = createStore({ createEventSource: () => new FakeEventSource(), presenceActive: () => false });
    await s.bootstrap(NO_CODE);
    await flushAll();
    s.state.screen = 'login';
    expect(await s.loginWithPassword('wrong-password')).toBe(LOGIN_TEXT_INVALID);
    expect(s.state.screen).toBe('login');
    stop(s);

    const ok = storeRouter();
    vi.stubGlobal('fetch', ok.fetchMock);
    const s2 = createStore({ createEventSource: () => new FakeEventSource(), presenceActive: () => false });
    expect(await s2.loginWithPassword('right-password')).toBeNull();
    await flushAll();
    expect(s2.state.screen).toBe('app');
    expect(s2.state.remoteSession).toBe(true);
    expect(ok.renews()).toBe(1);
    stop(s2);
  });

  it('429 — «через N мин» из retry_after_s', async () => {
    const r = storeRouter({ password: jsonResponse({ detail: 'too many attempts', retry_after_s: 240 }, 429) });
    vi.stubGlobal('fetch', r.fetchMock);
    const s = createStore({ createEventSource: () => new FakeEventSource(), presenceActive: () => false });
    expect(await s.loginWithPassword('x'.repeat(12))).toBe('Слишком много попыток — попробуй через 4 мин');
    expect(s.state.screen).toBe('loading');
  });
});

describe('Ф10-7 B4: выход из сессий в сторе', () => {
  function session(id: number, current: boolean): UiSession {
    return { id, via: 'local', created_ts: 1, last_used_ts: 2, expires_ts: 3, user_agent: null, current };
  }

  it('чужая сессия — экран остаётся; своя — экран входа, фон остановлен', async () => {
    const r = storeRouter();
    vi.stubGlobal('fetch', r.fetchMock);
    const s = createStore({ createEventSource: () => new FakeEventSource(), presenceActive: () => false });
    await s.bootstrap(NO_CODE);
    await flushAll();
    await s.revokeSession(session(4, false));
    expect(s.state.screen).toBe('app');
    await s.revokeSession(session(3, true));
    expect(s.state.screen).toBe('login');
    expect(api.getCsrf()).toBeNull();
    expect(r.calls).toContain('POST /api/v1/auth/sessions/3/revoke');
    stop(s);
  });

  it('«Выйти везде» — экран входа; «кроме этого» — остаёмся', async () => {
    const r = storeRouter();
    vi.stubGlobal('fetch', r.fetchMock);
    const s = createStore({ createEventSource: () => new FakeEventSource(), presenceActive: () => false });
    await s.bootstrap(NO_CODE);
    await flushAll();
    expect(await s.revokeAllSessions(true)).toBe(2);
    expect(s.state.screen).toBe('app');
    await s.revokeAllSessions(false);
    expect(s.state.screen).toBe('login');
    stop(s);
  });
});

// --- экраны входа на ноутбуке ---------------------------------------------------------------------------------

describe('Ф10-7 B2: на ноутбуке (localhost) экраны входа — как были', () => {
  it('LoginView и SessionExpiredView — `hub ui`, без поля пароля', () => {
    expect(window.location.hostname).toBe('localhost');
    for (const view of [LoginView, SessionExpiredView]) {
      const w = mount(view);
      expect(w.text()).toContain('hub ui');
      expect(w.find('input[type=password]').exists()).toBe(false);
      w.unmount();
    }
  });
});

// --- панель «Доступ и входы» -------------------------------------------------------------------------------

const REMOTE_OFF: RemoteStatus = {
  enabled: false,
  hosts: ['hub.example.com:8787'],
  password_set: true,
  password_set_ts: 1,
  listening: true,
  failures_24h: 3,
  blocked_until: null,
  tls: null, // Ф14-9: пары https нет
};

const SESSIONS: UiSession[] = [
  {
    id: 1,
    via: 'local',
    created_ts: Date.UTC(2026, 8, 25, 9, 0),
    last_used_ts: Date.UTC(2026, 8, 25, 10, 0),
    expires_ts: 0,
    user_agent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/128.0 Chrome/128.0 Safari/537.36',
    current: true,
  },
  {
    id: 2,
    via: 'remote',
    created_ts: Date.UTC(2026, 8, 25, 11, 0),
    last_used_ts: null,
    expires_ts: 0,
    user_agent: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Mobile Safari/537.36',
    current: false,
  },
  { id: 3, via: null, created_ts: null, last_used_ts: null, expires_ts: 0, user_agent: null, current: false },
];

describe('Ф10-7 B4: AccessPanel', () => {
  beforeEach(() => {
    store.state.remoteSession = false;
  });

  function mountPanel(remote: RemoteStatus = REMOTE_OFF) {
    vi.spyOn(api, 'getRemote').mockResolvedValue(remote);
    vi.spyOn(store, 'listSessions').mockResolvedValue(SESSIONS);
    return mount(AccessPanel, { attachTo: document.body });
  }

  it('входы: «этот», ноутбук/телефон/неизвестно, устройство, «Выйти» у строки', async () => {
    const w = mountPanel();
    await flushAll();
    const rows = w.findAll('li.access-session');
    expect(rows.map((r) => r.get('.access-session-what').text())).toEqual([
      'этот · ноутбук · Linux · Chromium',
      'телефон · Android · Chrome',
      'неизвестно · неизвестное устройство',
    ]);
    expect(rows[2]!.get('.access-session-when').text()).toBe('вошёл до 25.09 · был до 25.09');
    const revoke = vi.spyOn(store, 'revokeSession').mockResolvedValue(undefined);
    await rows[1]!.get('button.access-session-revoke').trigger('click');
    await flushAll();
    expect(revoke).toHaveBeenCalledWith(SESSIONS[1]);
    w.unmount();
  });

  it('«Выйти везде» — только после подтверждения', async () => {
    const w = mountPanel();
    await flushAll();
    const revokeAll = vi.spyOn(store, 'revokeAllSessions').mockResolvedValue(3);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    await w.get('button.access-revoke-all').trigger('click');
    await flushAll();
    expect(confirm).toHaveBeenCalledWith(CONFIRM_REVOKE_ALL);
    expect(revokeAll).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await w.get('button.access-revoke-all').trigger('click');
    await flushAll();
    expect(revokeAll).toHaveBeenCalledWith(false);
    await w.get('button.access-revoke-others').trigger('click');
    await flushAll();
    expect(revokeAll).toHaveBeenLastCalledWith(true);
    w.unmount();
  });

  it('ноутбук: включить, адрес, неверных за сутки; 422 при включении — подсказка', async () => {
    const w = mountPanel();
    await flushAll();
    expect(w.get('.access-state').text()).toBe('Выключен');
    expect(w.get('.access-failures').text()).toContain('Неверных паролей за сутки: 3');
    const put = vi.spyOn(api, 'putRemote').mockResolvedValue({ ...REMOTE_OFF, enabled: true });
    await w.get('button.access-enable').trigger('click');
    await flushAll();
    expect(put).toHaveBeenCalledWith({ enabled: true });
    expect(w.get('.access-state').text()).toBe('Включён, адрес hub.example.com:8787');

    put.mockRejectedValueOnce(new api.ApiError(422, 'no', null));
    await w.get('button.access-disable').trigger('click');
    await flushAll();
    expect(w.get('.access-notice').text()).toBe('Сначала задай пароль и адрес.');

    put.mockResolvedValue({ ...REMOTE_OFF, enabled: true, hosts: ['a.example.com:8787', 'b.example.com:8787'] });
    await w.get('input.access-hosts-input').setValue('A.example.com:8787, b.example.com:8787');
    await w.get('form.access-hosts').trigger('submit');
    await flushAll();
    expect(put).toHaveBeenLastCalledWith({ hosts: ['a.example.com:8787', 'b.example.com:8787'] });
    w.unmount();
  });

  it('ноутбук: смена пароля — два поля, совпадение, 10+; предупреждение «телефоны выйдут»', async () => {
    const w = mountPanel();
    await flushAll();
    const setPw = vi.spyOn(api, 'setRemotePassword').mockResolvedValue({ ok: true, revoked_remote: 2 });
    await w.get('button.access-password-open').trigger('click');
    expect(w.text()).toContain('Телефоны выйдут');
    expect(w.get('input.access-pw1').attributes('autocomplete')).toBe('new-password');

    await w.get('input.access-pw1').setValue('0123456789');
    await w.get('input.access-pw2').setValue('0123456780');
    await w.get('form.access-password-form').trigger('submit');
    await flushAll();
    expect(setPw).not.toHaveBeenCalled();
    expect(w.get('.access-password-form .access-error').text()).toBe('Пароли не совпадают');

    await w.get('input.access-pw1').setValue('short');
    await w.get('input.access-pw2').setValue('short');
    await w.get('form.access-password-form').trigger('submit');
    await flushAll();
    expect(setPw).not.toHaveBeenCalled();

    await w.get('input.access-pw1').setValue('correct horse battery');
    await w.get('input.access-pw2').setValue('correct horse battery');
    await w.get('form.access-password-form').trigger('submit');
    await flushAll();
    expect(setPw).toHaveBeenCalledWith('correct horse battery');
    expect(w.get('.access-notice').text()).toBe('Пароль сохранён. Вышло телефонов: 2.');
    expect(w.find('form.access-password-form').exists()).toBe(false);
    w.unmount();
  });

  it('пароль не задан — кнопка «Задать пароль», без предупреждения; удалённый слушатель не поднят — строка', async () => {
    const w = mountPanel({ ...REMOTE_OFF, password_set: false, listening: false });
    await flushAll();
    expect(w.get('button.access-password-open').text()).toBe('Задать пароль');
    await w.get('button.access-password-open').trigger('click');
    expect(w.text()).not.toContain('Телефоны выйдут');
    expect(w.text()).toContain('Удалённый вход не поднят');
    w.unmount();
  });

  it('телефон: доступ — только строка состояния, без правки', async () => {
    store.state.remoteSession = true;
    const w = mountPanel({ ...REMOTE_OFF, enabled: true });
    await flushAll();
    expect(w.get('.access-state').text()).toBe('Включён, адрес hub.example.com:8787');
    expect(w.find('button.access-enable').exists()).toBe(false);
    expect(w.find('button.access-disable').exists()).toBe(false);
    expect(w.find('form.access-hosts').exists()).toBe(false);
    expect(w.find('button.access-password-open').exists()).toBe(false);
    expect(w.findAll('li.access-session')).toHaveLength(3);
    w.unmount();
  });
});
