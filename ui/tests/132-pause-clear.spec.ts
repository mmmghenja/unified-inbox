// Ф66-3 F: пункт «Снять паузу лимита» в меню «⋯» — виден, только когда хоть одна сессия хаба ждёт по причине
// `paused`; подтверждение одной строкой; клик шлёт POST /work/pause/clear.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession } from '../src/api/types';
import TopMenu from '../src/components/TopMenu.vue';
import { store } from '../src/store/instance';
import { createSessionsState } from '../src/store/sessions';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

let sent: { method: string; path: string }[] = [];

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.sessions = createSessionsState();
  sent = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      sent.push({ method: init?.method ?? 'GET', path: url.pathname });
      if (url.pathname === '/api/v1/sessions') return json([]);
      return json({ ok: true, cleared_ts: 1 });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function session(id: number, wait: string | null): HubSession {
  return { id, wait_reason: wait } as unknown as HubSession;
}

describe('Ф66-3 F: «Снять паузу лимита»', () => {
  it('нет сессий на паузе — пункта нет', async () => {
    store.state.sessions.items = { 1: session(1, null), 2: session(2, 'heavy') };
    const menu = mount(TopMenu, { attachTo: document.body });
    await menu.get('.top-menu-button').trigger('click');
    expect(menu.find('[data-test="pause-clear"]').exists()).toBe(false);
    menu.unmount();
  });

  it('есть сессия на паузе: пункт виден, отказ в подтверждении ничего не шлёт', async () => {
    store.state.sessions.items = { 1: session(1, 'paused') };
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const menu = mount(TopMenu, { attachTo: document.body });
    await menu.get('.top-menu-button').trigger('click');
    await menu.get('[data-test="pause-clear"]').trigger('click');
    expect(confirm).toHaveBeenCalledWith('Снять паузу у всех сессий? Если лимит ещё не сброшен, Claude снова его покажет.');
    expect(sent.some((s) => s.path === '/api/v1/work/pause/clear')).toBe(false);
    menu.unmount();
  });

  it('подтверждение -> POST /work/pause/clear', async () => {
    store.state.sessions.items = { 1: session(1, 'paused') };
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const menu = mount(TopMenu, { attachTo: document.body });
    await menu.get('.top-menu-button').trigger('click');
    await menu.get('[data-test="pause-clear"]').trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.method === 'POST' && s.path === '/api/v1/work/pause/clear')).toHaveLength(1);
    menu.unmount();
  });
});

describe('Ф66-3 B: поля окон групп WhatsApp на телефоне', () => {
  it('поле участника и поле ссылки — 16 px при ширине < 600 (браузер не приближает страницу)', () => {
    const css = readFileSync(resolve(__dirname, '../src/styles/board.css'), 'utf8');
    const block = css.match(/@media \(width < 600px\) \{\s*\.group-people-input,\s*\.group-link-field \{([^}]*)\}/);
    expect(block).not.toBeNull();
    expect(block![1]).toContain('font-size: 16px');
  });
});
