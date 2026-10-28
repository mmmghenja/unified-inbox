// Ф58-1 C (`docs/architecture.md`): ответ прямо из окна «открыть окном» карточки очереди. Блок
// ответов (кнопки, текстовое поле, ошибка) переезжает в окно `Teleport`-ом: одно состояние, тот же запрос; после
// ответа карточка уходит из очереди и окно закрывается вместе с ней; закрыли окно — блок снова в карточке.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OwnerQueue } from '../src/api/types';
import AgentQueue from '../src/components/AgentQueue.vue';
import { store } from '../src/store/instance';
import { createOwnerState } from '../src/store/owner_channel';

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const NOW = 1760360000000;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}
let sent: Sent[] = [];
let current: OwnerQueue;
let failAnswer = false;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  current = JSON.parse(JSON.stringify(FIXTURE)) as OwnerQueue;
  const turn = current.items.find((i) => i.ref === 'turn:11:4')!;
  turn.full.answer = `**начало**\n${'длинный текст '.repeat(100)}`;
  current.items = [turn];
  sent = [];
  failAnswer = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      sent.push(s);
      if (s.path === '/api/v1/owner_channel/queue') return json(current);
      if (s.method === 'POST' && decodeURIComponent(s.path).endsWith('/answer')) {
        if (failAnswer) return json({ detail: 'Нельзя так ответить.' }, 422);
        current = { ...current, items: [] };
        return json({ ref: 'turn:11:4', status: 'ok' });
      }
      if (s.path === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      return json({});
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

async function mountOpened() {
  const w = mount(AgentQueue, { props: { nowMs: NOW }, attachTo: document.body });
  await flushPromises();
  await w.get('.owner-open').trigger('click');
  await flushPromises();
  return w;
}

function modalAnswer(): Element | null {
  return document.body.querySelector('.owner-full-modal .owner-modal-answer');
}

describe('58-1 C ответ из окна карточки', () => {
  it('без окна блок ответов стоит в карточке', async () => {
    const w = mount(AgentQueue, { props: { nowMs: NOW }, attachTo: document.body });
    await flushPromises();
    expect(w.find('.owner-card .owner-answer-block [data-action="reply"]').exists()).toBe(true);
    expect(document.body.querySelector('.owner-full-modal')).toBeNull();
    w.unmount();
  });

  it('в окне есть кнопки ответа, в карточке их на это время нет', async () => {
    const w = await mountOpened();
    expect(modalAnswer()!.querySelector('[data-action="reply"]')).not.toBeNull();
    expect(w.find('.owner-card [data-action="reply"]').exists()).toBe(false);
    w.unmount();
  });

  it('ответ из окна шлёт тот же запрос, что из карточки; после него окна нет', async () => {
    const w = await mountOpened();
    (modalAnswer()!.querySelector('[data-action="reply"]') as HTMLButtonElement).click();
    await flushPromises();
    const area = modalAnswer()!.querySelector('textarea.owner-text-input') as HTMLTextAreaElement;
    expect(area).not.toBeNull();
    area.value = 'сделай ещё X';
    area.dispatchEvent(new Event('input'));
    await flushPromises();
    (modalAnswer()!.querySelector('[data-test="owner-text-send"]') as HTMLButtonElement).click();
    await flushPromises();
    const posts = sent.filter((s) => s.method === 'POST' && decodeURIComponent(s.path).endsWith('/answer'));
    expect(posts).toHaveLength(1);
    expect(decodeURIComponent(posts[0]!.path)).toBe('/api/v1/owner_channel/turn:11:4/answer');
    expect(posts[0]!.body).toEqual({ action: 'reply', text: 'сделай ещё X' });
    expect(document.body.querySelector('.owner-full-modal')).toBeNull();
    w.unmount();
  });

  it('ошибка ответа видна в окне', async () => {
    failAnswer = true;
    const w = await mountOpened();
    (modalAnswer()!.querySelector('[data-action="reply"]') as HTMLButtonElement).click();
    await flushPromises();
    const area = modalAnswer()!.querySelector('textarea.owner-text-input') as HTMLTextAreaElement;
    area.value = 'текст';
    area.dispatchEvent(new Event('input'));
    await flushPromises();
    (modalAnswer()!.querySelector('[data-test="owner-text-send"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(modalAnswer()!.querySelector('[role="alert"]')!.textContent).toContain('Нельзя так ответить.');
    w.unmount();
  });

  it('закрыли окно — блок ответов снова в карточке', async () => {
    const w = await mountOpened();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(document.body.querySelector('.owner-full-modal')).toBeNull();
    expect(w.find('.owner-card .owner-answer-block [data-action="reply"]').exists()).toBe(true);
    w.unmount();
  });
});
