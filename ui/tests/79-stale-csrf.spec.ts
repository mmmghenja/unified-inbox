// Ф38-4: устаревшая вкладка. csrf считается из cookie, cookie общая на все вкладки: вход в другой вкладке (второе
// окно `hub ui`) меняет cookie, и запись из этой вкладки получает от middleware ядра ПУСТОЙ 403. Клиент перечитывает
// csrf (`GET /auth/me`) и повторяет запрос один раз. Живой случай 05.10.2026: «Отклонить» на карточке заявки —
// «Не отправилось — попробуй ещё раз.» во второй вкладке хаба.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** Отказ middleware (Origin/CSRF): пустое тело, без content-type. */
function bare403(): Response {
  return new Response(null, { status: 403 });
}

interface Sent {
  method: string;
  path: string;
  csrf: string | null;
}

const ME = { token_id: 1, kind: 'ui', scopes: [], project_ids: null };

/** Ядро с cookie новой сессии: запись проходит только с `valid`, `/auth/me` отдаёт `served`. */
function core(valid: string, served: string | null = valid, meStatus = 200) {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: new URL(input, 'http://localhost').pathname,
      csrf: headers['X-Hub-CSRF'] ?? null,
    };
    sent.push(s);
    if (s.path === '/api/v1/auth/me') {
      return meStatus === 200 ? jsonResponse({ ...ME, csrf: served }) : jsonResponse({ detail: 'unauthorized' }, meStatus);
    }
    if (s.method === 'GET') return jsonResponse([]);
    return s.csrf === valid ? jsonResponse({ id: 2, status: 'declined' }) : bare403();
  });
  return { sent, fetchMock };
}

const writes = (sent: Sent[]) => sent.filter((s) => s.method !== 'GET');
const meCalls = (sent: Sent[]) => sent.filter((s) => s.path === '/api/v1/auth/me');

beforeEach(() => {
  api.setCsrf('old');
  api.setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Ф38-4: запись из устаревшей вкладки', () => {
  it('пустой 403 -> csrf перечитан, запрос повторён один раз с новым csrf и прошёл', async () => {
    const { sent, fetchMock } = core('new');
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.declineRuleRequest(2)).resolves.toMatchObject({ status: 'declined' });
    expect(writes(sent).map((s) => s.csrf)).toEqual(['old', 'new']);
    expect(meCalls(sent)).toHaveLength(1);
    expect(api.getCsrf()).toBe('new');
  });

  it('две записи разом -> одно перечитывание, обе повторены', async () => {
    const { sent, fetchMock } = core('new');
    vi.stubGlobal('fetch', fetchMock);
    await Promise.all([api.declineRuleRequest(2), api.approveRuleRequest(3)]);
    expect(meCalls(sent)).toHaveLength(1);
    expect(writes(sent).map((s) => s.csrf).sort()).toEqual(['new', 'new', 'old', 'old']);
  });

  it('csrf после перечитывания тот же -> повтора нет, ошибка как была', async () => {
    const { sent, fetchMock } = core('new', 'old');
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.declineRuleRequest(2)).rejects.toMatchObject({ status: 403 });
    expect(writes(sent)).toHaveLength(1);
    expect(meCalls(sent)).toHaveLength(1);
  });

  it('повтор тоже получил пустой 403 -> третьей попытки нет', async () => {
    const { sent, fetchMock } = core('другой', 'new');
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.declineRuleRequest(2)).rejects.toMatchObject({ status: 403 });
    expect(writes(sent).map((s) => s.csrf)).toEqual(['old', 'new']);
    expect(meCalls(sent)).toHaveLength(1);
  });

  it('403 самой ручки (с detail) -> csrf не перечитывается, повтора нет', async () => {
    const sent: Sent[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        sent.push({ method: init?.method ?? 'GET', path: new URL(input, 'http://localhost').pathname, csrf: null });
        return jsonResponse({ detail: 'только из интерфейса хаба' }, 403);
      }),
    );
    await expect(api.declineRuleRequest(2)).rejects.toMatchObject({ status: 403, message: 'только из интерфейса хаба' });
    expect(sent).toHaveLength(1);
  });

  it('сессии больше нет (/auth/me 401) -> обработчик «сессия истекла», повтора нет', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    const { sent, fetchMock } = core('new', 'new', 401);
    vi.stubGlobal('fetch', fetchMock);
    await expect(api.declineRuleRequest(2)).rejects.toMatchObject({ status: 403 });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(writes(sent)).toHaveLength(1);
  });

  it('чтение с пустым 403 csrf не перечитывает', async () => {
    const sent: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        sent.push(new URL(input, 'http://localhost').pathname);
        return bare403();
      }),
    );
    await expect(api.listRuleRequests()).rejects.toMatchObject({ status: 403 });
    expect(sent).toEqual(['/api/v1/rule-requests']);
  });

  it('загрузка файла: пустой 403 -> перечитывание и один повтор с новым csrf', async () => {
    const { sent, fetchMock } = core('new');
    vi.stubGlobal('fetch', fetchMock);
    const file = new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' });
    await expect(api.uploadFile(5, file, 'a.png')).resolves.toBeTruthy();
    const uploads = sent.filter((s) => s.path === '/api/v1/uploads');
    expect(uploads.map((s) => s.csrf)).toEqual(['old', 'new']);
    expect(meCalls(sent)).toHaveLength(1);
  });
});
