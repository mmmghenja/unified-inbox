// Пункт 10 спеки: код из хеша уходит в POST, хеш вычищен `replaceState` ДО запроса; CSRF не попадает ни в
// одно хранилище. Проверяется на выделенных функциях (`readAndClearLoginCode`, `loginFromLocationHash`) с
// лёгким фейком окна — без реального `window`/EventSource/остального `Store.bootstrap()`.
import { describe, expect, it, vi } from 'vitest';

import {
  type LoginWindowLike,
  loginFromLocationHash,
  readAndClearLoginCode,
} from '../src/store/store';

function fakeWindow(hash: string): LoginWindowLike & { replaceStateCalls: unknown[][] } {
  const replaceStateCalls: unknown[][] = [];
  return {
    location: { hash, pathname: '/login', search: '' },
    history: {
      replaceState: (...args: unknown[]) => {
        replaceStateCalls.push(args);
      },
    },
    replaceStateCalls,
  };
}

describe('10 Вход: чтение и очистка кода из хеша', () => {
  it('код есть -> возвращается и хеш вычищается через replaceState', () => {
    const win = fakeWindow('#code=secret-code-123');
    const code = readAndClearLoginCode(win);
    expect(code).toBe('secret-code-123');
    expect(win.replaceStateCalls).toHaveLength(1);
    expect(win.replaceStateCalls[0]).toEqual([null, '', '/login']);
  });

  it('хеша нет -> null, replaceState не вызывается', () => {
    const win = fakeWindow('');
    expect(readAndClearLoginCode(win)).toBeNull();
    expect(win.replaceStateCalls).toHaveLength(0);
  });
});

describe('10 Вход: порядок — replaceState ДО POST', () => {
  it('хеш вычищается раньше, чем уходит запрос на вход', async () => {
    const order: string[] = [];
    const win: LoginWindowLike = {
      location: { hash: '#code=xyz', pathname: '/login', search: '' },
      history: {
        replaceState: () => {
          order.push('replaceState');
        },
      },
    };
    const loginFn = vi.fn(async (code: string) => {
      order.push('POST /auth/session');
      expect(code).toBe('xyz');
      return { csrf: 'c', expires_ts: 0 };
    });

    const result = await loginFromLocationHash(win, loginFn);

    expect(result).toEqual({ attempted: true, ok: true });
    expect(order).toEqual(['replaceState', 'POST /auth/session']);
  });

  it('неверный код -> attempted=true, ok=false, хеш всё равно вычищен', async () => {
    const win = fakeWindow('#code=bad');
    const loginFn = vi.fn(async () => {
      throw new Error('401');
    });

    const result = await loginFromLocationHash(win, loginFn);

    expect(result).toEqual({ attempted: true, ok: false });
    expect(win.replaceStateCalls).toHaveLength(1);
  });

  it('без кода в хеше — вход по хешу не пытается сходить в сеть', async () => {
    const win = fakeWindow('');
    const loginFn = vi.fn();
    const result = await loginFromLocationHash(win, loginFn);
    expect(result).toEqual({ attempted: false, ok: false });
    expect(loginFn).not.toHaveBeenCalled();
  });
});

describe('10 Вход: CSRF не попадает ни в одно хранилище', () => {
  it('после успешного входа localStorage/sessionStorage/document.cookie пусты', async () => {
    localStorage.clear();
    sessionStorage.clear();

    const win = fakeWindow('#code=xyz');
    await loginFromLocationHash(win, async () => ({ csrf: 'super-secret-csrf', expires_ts: 0 }));

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe('');
  });
});
