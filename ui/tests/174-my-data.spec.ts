// Ф82-1 (`docs/architecture.md`): окно «⋯ → Мои данные» — чипы наборов, поля без значений,
// «показать» по клику (значение только в памяти окна), правка, «пусто в этом проекте», удаление, добавление,
// журнал; пункт меню «⋯».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { MyDataField } from '../src/api/types';
import MyDataPanel from '../src/components/MyDataPanel.vue';
import TopMenu from '../src/components/TopMenu.vue';

const SECRET = '4510 123456';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function field(overrides: Partial<MyDataField> = {}): MyDataField {
  return {
    key: 'passport_number',
    label: 'номер паспорта',
    source: 'common',
    filled: true,
    set: true,
    length: SECRET.length,
    by: 'owner_channel',
    ts: new Date(2026, 9, 22, 12, 30).getTime(),
    ...overrides,
  };
}

const OVERVIEW = {
  sets: [
    { scope: 'common', project_id: null, title: 'Общее', count: 1, broken: false },
    { scope: '3', project_id: 3, title: 'Umbrella', count: 0, broken: false },
  ],
  catalog: [
    { key: 'surname', label: 'фамилия' },
    { key: 'passport_number', label: 'номер паспорта' },
  ],
  value_max: 500,
};

type Handler = (method: string, path: string, body: unknown, query: URLSearchParams) => Response | undefined;

function stub(handler: Handler = () => undefined): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    const own = handler(method, url.pathname, body, url.searchParams);
    if (own) return own;
    if (url.pathname === '/api/v1/my-data') return json(OVERVIEW);
    if (url.pathname === '/api/v1/my-data/common') return json({ scope: 'common', fields: [field()] });
    if (url.pathname === '/api/v1/my-data/3') {
      return json({ scope: '3', fields: [field({ from_common: true }), field({ key: 'inn', label: 'ИНН', source: 'project', filled: false, length: 0, from_common: false })] });
    }
    if (url.pathname === '/api/v1/my-data/common/passport_number' && method === 'GET') {
      return json({ key: 'passport_number', value: SECRET, from_common: false });
    }
    if (url.pathname === '/api/v1/my-data/journal') return json({ scope: url.searchParams.get('scope'), items: ['- 22.10.2026 12:00 — example.org (login) — вводил: phone'] });
    return json({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function calls(fetchMock: ReturnType<typeof vi.fn>, method: string): Array<{ path: string; body: unknown }> {
  return fetchMock.mock.calls
    .filter((c) => ((c[1] as RequestInit | undefined)?.method ?? 'GET') === method)
    .map((c) => ({ path: new URL(String(c[0]), 'http://localhost').pathname, body: (c[1] as RequestInit | undefined)?.body ? JSON.parse(String((c[1] as RequestInit).body)) : undefined }));
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф82-1: окно «Мои данные»', () => {
  it('чипы наборов, поле без значения; «показать» — один запрос, значение не в localStorage и не в консоли', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const log = vi.spyOn(console, 'log');
    const fetchMock = stub();
    const w = mount(MyDataPanel);
    await flushPromises();
    const chips = w.findAll('[data-test="my-data-chip"]');
    expect(chips.map((c) => c.text())).toEqual(['Общее1', 'Umbrella']);
    const row = w.get('[data-test="my-data-row"]');
    expect(row.text()).toContain('номер паспорта');
    expect(row.text()).toContain(`••• ${SECRET.length} зн.`);
    expect(row.text()).toContain('Владелец · 22.10.2026');
    expect(w.html()).not.toContain(SECRET);

    await row.get('[data-test="my-data-show"]').trigger('click');
    await flushPromises();
    expect(w.get('[data-test="my-data-shown"]').text()).toBe(SECRET);
    expect(calls(fetchMock, 'GET').filter((c) => c.path === '/api/v1/my-data/common/passport_number')).toHaveLength(1);
    expect(w.get('[data-test="my-data-show"]').text()).toBe('скрыть');
    await w.get('[data-test="my-data-show"]').trigger('click');
    expect(w.html()).not.toContain(SECRET);

    for (const c of setItem.mock.calls) expect(String(c[1])).not.toContain(SECRET);
    for (const c of log.mock.calls) expect(JSON.stringify(c)).not.toContain(SECRET);
    w.unmount();
  });

  it('проект: «из общего», «пусто в этом проекте» шлёт пустую строку, удаления у поля из общего нет', async () => {
    const fetchMock = stub();
    const w = mount(MyDataPanel);
    await flushPromises();
    await w.findAll('[data-test="my-data-chip"]')[1]!.trigger('click');
    await flushPromises();
    const rows = w.findAll('[data-test="my-data-row"]');
    expect(rows[0]!.find('[data-test="my-data-from-common"]').exists()).toBe(true);
    expect(rows[0]!.find('[data-test="my-data-delete"]').exists()).toBe(false);
    // явно пустое поле проекта: подпись и удаление (снова будет общее), без второй «пусто»
    expect(rows[1]!.text()).toContain('пусто в этом проекте');
    expect(rows[1]!.find('[data-test="my-data-empty"]').exists()).toBe(false);
    expect(rows[1]!.find('[data-test="my-data-delete"]').exists()).toBe(true);
    expect(rows[1]!.find('[data-test="my-data-show"]').exists()).toBe(false);

    await rows[0]!.get('[data-test="my-data-empty"]').trigger('click');
    await flushPromises();
    expect(calls(fetchMock, 'PUT')).toEqual([{ path: '/api/v1/my-data/3/passport_number', body: { value: '' } }]);
    w.unmount();
  });

  it('изменить: поле с текущим значением, Enter — PUT; удалить — со вторым нажатием', async () => {
    const fetchMock = stub((method) => {
      if (method === 'PUT') return json(field());
      if (method === 'DELETE') return new Response(null, { status: 204 });
      return undefined;
    });
    const w = mount(MyDataPanel);
    await flushPromises();
    await w.get('[data-test="my-data-change"]').trigger('click');
    await flushPromises();
    const input = w.get('[data-test="my-data-edit"]');
    expect((input.element as HTMLInputElement).value).toBe(SECRET);
    await input.setValue('4510 000000');
    await input.trigger('keydown', { key: 'Enter' });
    await flushPromises();
    expect(calls(fetchMock, 'PUT')).toEqual([{ path: '/api/v1/my-data/common/passport_number', body: { value: '4510 000000' } }]);
    expect(w.find('[data-test="my-data-edit"]').exists()).toBe(false);

    await w.get('[data-test="my-data-delete"]').trigger('click');
    expect(calls(fetchMock, 'DELETE')).toHaveLength(0);
    expect(w.get('[data-test="my-data-delete"]').text()).toBe('точно удалить?');
    await w.get('[data-test="my-data-delete"]').trigger('click');
    await flushPromises();
    expect(calls(fetchMock, 'DELETE').map((c) => c.path)).toEqual(['/api/v1/my-data/common/passport_number']);
    w.unmount();
  });

  it('добавить поле: из каталога (без уже заданных) и свой ключ; плохой ключ — текстом без запроса', async () => {
    const fetchMock = stub((method) => (method === 'PUT' ? json(field()) : undefined));
    const w = mount(MyDataPanel);
    await flushPromises();
    await w.get('[data-test="my-data-add"]').trigger('click');
    const options = w.findAll('[data-test="my-data-add-key"] option').map((o) => o.text());
    expect(options).toEqual(['фамилия', 'Свой ключ…']);
    await w.get('[data-test="my-data-add-value"]').setValue('Иванов');
    await w.get('[data-test="my-data-add-save"]').trigger('click');
    await flushPromises();
    expect(calls(fetchMock, 'PUT')).toEqual([{ path: '/api/v1/my-data/common/surname', body: { value: 'Иванов' } }]);

    await w.get('[data-test="my-data-add"]').trigger('click');
    await w.get('[data-test="my-data-add-key"]').setValue('__custom__');
    await w.get('[data-test="my-data-add-custom"]').setValue('Shop-Login');
    await w.get('[data-test="my-data-add-value"]').setValue('x');
    await w.get('[data-test="my-data-add-save"]').trigger('click');
    await flushPromises();
    expect(w.get('[data-test="my-data-add-error"]').text()).toContain('латиница');
    expect(calls(fetchMock, 'PUT')).toHaveLength(1);
    await w.get('[data-test="my-data-add-custom"]').setValue('shop_login');
    await w.get('[data-test="my-data-add-save"]').trigger('click');
    await flushPromises();
    expect(calls(fetchMock, 'PUT')[1]).toEqual({ path: '/api/v1/my-data/common/shop_login', body: { value: 'x' } });
    w.unmount();
  });

  it('битый файл — 409 текстом с именем файла; журнал свёрнут и грузится при раскрытии', async () => {
    const fetchMock = stub((method, path) =>
      path === '/api/v1/my-data/common' ? json({ detail: 'broken', file: 'me.json' }, 409) : undefined,
    );
    const w = mount(MyDataPanel);
    await flushPromises();
    expect(w.get('[data-test="my-data-error"]').text()).toContain('me.json');
    expect(calls(fetchMock, 'GET').some((c) => c.path === '/api/v1/my-data/journal')).toBe(false);
    const details = w.get('[data-test="my-data-journal"]');
    (details.element as HTMLDetailsElement).open = true;
    await details.trigger('toggle');
    await flushPromises();
    expect(w.get('[data-test="my-data-journal-line"]').text()).toContain('вводил: phone');
    w.unmount();
  });
});

describe('Ф82-1: пункт меню «⋯»', () => {
  it('«Мои данные» открывает окно', async () => {
    stub();
    const w = mount(TopMenu, { attachTo: document.body });
    await w.get('.top-menu-button').trigger('click');
    await w.get('[data-test="my-data-open"]').trigger('click');
    await flushPromises();
    expect(w.find('.my-data-panel').exists()).toBe(true);
    w.unmount();
  });
});
