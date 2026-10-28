// @vitest-environment-options {"url": "http://hub.example.com:8787/"}
// Ф10-7 B2: страница открыта не на `127.0.0.1`/`localhost` (телефон через VPN и туннель) — экраны «нет входа» и
// «сессия истекла» показывают поле пароля; ошибки 401/429/403/421/сеть — своими текстами.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import LoginView from '../src/components/LoginView.vue';
import SessionExpiredView from '../src/components/SessionExpiredView.vue';
import { store } from '../src/store/instance';
import { LOGIN_TEXT_INVALID, LOGIN_TEXT_UNAVAILABLE } from '../src/util/remote';

afterEach(() => {
  vi.restoreAllMocks();
});

describe.each([
  ['LoginView', LoginView],
  ['SessionExpiredView', SessionExpiredView],
])('Ф10-7 B2: %s с телефона', (_name, view) => {
  it('поле пароля в форме без action, `hub ui` не показывается', () => {
    expect(window.location.hostname).toBe('hub.example.com');
    const w = mount(view);
    const input = w.get('input[type=password]');
    expect(input.attributes('autocomplete')).toBe('current-password');
    const form = w.get('form.password-login');
    expect(form.attributes('action')).toBeUndefined();
    expect(form.get('button[type=submit]').text()).toBe('Войти');
    expect(w.text()).not.toContain('hub ui');
    w.unmount();
  });

  it('«Войти» — вход паролем; ошибка — строкой под полем', async () => {
    const login = vi
      .spyOn(store, 'loginWithPassword')
      .mockResolvedValueOnce(LOGIN_TEXT_INVALID)
      .mockResolvedValueOnce(LOGIN_TEXT_UNAVAILABLE)
      .mockResolvedValueOnce(null);
    const w = mount(view);
    await w.get('input[type=password]').setValue('wrong password');
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(login).toHaveBeenCalledWith('wrong password');
    expect(w.get('.password-login-error').text()).toBe('Неверный пароль');

    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.get('.password-login-error').text()).toBe('Доступ с телефона выключен или ноутбук недоступен');

    await w.get('form').trigger('submit');
    await flushPromises();
    expect(w.find('.password-login-error').exists()).toBe(false);
    expect((w.get('input[type=password]').element as HTMLInputElement).value).toBe('');
    w.unmount();
  });

  it('пустой пароль — запроса нет', async () => {
    const login = vi.spyOn(store, 'loginWithPassword');
    const w = mount(view);
    await w.get('form').trigger('submit');
    await flushPromises();
    expect(login).not.toHaveBeenCalled();
    w.unmount();
  });
});
