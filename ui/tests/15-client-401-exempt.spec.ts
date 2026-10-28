// F10 (приёмка 6b, дефект найден чтением кода): 401 на `POST /auth/session` (неверный/просроченный код
// входа) раньше безусловно дёргал общий `unauthorizedHandler` -> экран мигал «сессия истекла» вместо
// нормального результата попытки входа. Плюс: стартовый `authMe()` в `bootstrap()` тоже не должен дёргать
// общий обработчик на 401 (это «сессии ещё не было», а не «истекла») — но ПОВТОРНЫЙ `authMe()` из проверки
// при реконнекте SSE (F2) обязан его дёргать. Реализовано через `silent401` — флаг ОДНОГО вызова, не пути.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  api.setCsrf(null);
  api.setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('F10: 401 на /auth/session никогда не дёргает общий обработчик', () => {
  it('неверный/просроченный код входа -> обработчик не вызван, ApiError всё равно брошен', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ detail: 'invalid or expired code' }, 401)),
    );

    await expect(api.login('bad-code')).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});

describe('F10: 401 на /auth/me — только со `silent401` не дёргает обработчик', () => {
  it('без silent401 (проверка сессии при реконнекте, F2) -> обработчик вызван', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401)));

    await expect(api.authMe()).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('с silent401=true (стартовая проверка в bootstrap()) -> обработчик НЕ вызван', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401)));

    await expect(api.authMe({ silent401: true })).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});

describe('F10: 401 на любой другой ручке — обработчик как обычно', () => {
  it('/api/v1/projects 401 -> обработчик вызван', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401)));

    await expect(api.listProjects()).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

describe('Ф10-7: 421 (доступ с телефона выключен) — как 401', () => {
  it('/api/v1/projects 421 -> обработчик вызван (экран входа, переписка уходит с экрана)', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 421 })));

    await expect(api.listProjects()).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('421 на входе паролем — не обработчик, а ошибка формы', async () => {
    const handler = vi.fn();
    api.setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 421 })));

    await expect(api.loginWithPassword('0123456789')).rejects.toBeInstanceOf(api.ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});
