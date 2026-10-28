// Ф65-1, правки после живой проверки 15.10 (`docs/architecture.md`):
// (2б) строка хода поручения не застывает — пока у карточки открыто поручение, очередь перечитывается раз в 5 с;
// (3) блок возврата: «Агент вернул тебе: …», поле «Скажи агенту, как решить…», обычная кнопка «Поручить агенту».
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OwnerItem, OwnerQueue } from '../src/api/types';
import ErrandReturned from '../src/components/ErrandReturned.vue';
import OwnerCard from '../src/components/OwnerCard.vue';
import { store } from '../src/store/instance';
import { OWNER_ERRAND_POLL_MS } from '../src/store/store';
import { createOwnerState } from '../src/store/owner_channel';
import { returnedLine } from '../src/util/owner_channel';

const NOW = 1760360000000;
const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function card(errand: OwnerItem['errand']): OwnerItem {
  return {
    ref: 'turn:3', kind: 'turn', project_id: 5, project_name: 'Globex', session_id: 1, role: 'руководитель',
    role_no: 1, brief: 'выкат 10.8.3 готов', full: { answer: 'Выкат 10.8.3 готов. Напиши «накатывай».' },
    created_ts: NOW - 60_000, until_ts: null, in_channel: true, hold_ts: null, errand, errand_returned: null,
    agent_ask: null, auto_ok: true,
    answers: [
      { action: 'reply', label: 'Ответить', needs_text: true, takes_text: true },
      { action: 'ok', label: 'Принял', needs_text: false, takes_text: false },
    ],
    agent_actions: [],
  } as unknown as OwnerItem;
}

const ERRAND = { id: 9, action: 'solve', label: 'Реши', text: null, created_ts: NOW - 20_000, progress: 'думаю, 10 с…', progress_ts: NOW, auto: false };

interface Sent {
  method: string;
  path: string;
  body: unknown;
}
let sent: Sent[] = [];
let queue: OwnerItem[] = [];
const queueGets = () => sent.filter((s) => s.path === '/api/v1/owner_channel/queue').length;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  sent = [];
  queue = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      sent.push(s);
      if (s.path === '/api/v1/owner_channel/queue') return json({ items: queue, seq: 1, agent: { online: true, seen_ts: NOW } });
      if (s.path === '/api/v1/human-requests') return json([]);
      return json({ ok: true });
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('2б ход поручения в карточке обновляется', () => {
  it('пока поручение открыто — очередь раз в 5 с, строка хода меняется; поручения нет — опрос стоит', async () => {
    vi.useFakeTimers();
    queue = [card({ ...ERRAND })];
    await store.loadOwner();
    expect(queueGets()).toBe(1);
    queue = [card({ ...ERRAND, progress: 'думаю, 20 с…' })];
    await vi.advanceTimersByTimeAsync(OWNER_ERRAND_POLL_MS + 10);
    expect(queueGets()).toBe(2);
    expect(store.state.owner_channel.items[0]!.errand?.progress).toBe('думаю, 20 с…');
    queue = [card(null)]; // агент ответил — поручение закрыто
    await vi.advanceTimersByTimeAsync(OWNER_ERRAND_POLL_MS + 10);
    expect(queueGets()).toBe(3);
    await vi.advanceTimersByTimeAsync(OWNER_ERRAND_POLL_MS * 4);
    expect(queueGets()).toBe(3);
  });

  it('без открытых поручений частого опроса нет', async () => {
    vi.useFakeTimers();
    queue = [card(null)];
    await store.loadOwner();
    await vi.advanceTimersByTimeAsync(OWNER_ERRAND_POLL_MS * 3);
    expect(queueGets()).toBe(1);
  });

  it('частые загрузки не множат таймеры', async () => {
    vi.useFakeTimers();
    queue = [card({ ...ERRAND })];
    await store.loadOwner();
    await store.loadOwner();
    await store.loadOwner();
    expect(queueGets()).toBe(3);
    await vi.advanceTimersByTimeAsync(OWNER_ERRAND_POLL_MS + 10);
    expect(queueGets()).toBe(4);
    queue = [];
    await vi.advanceTimersByTimeAsync(OWNER_ERRAND_POLL_MS + 10);
    await vi.advanceTimersByTimeAsync(OWNER_ERRAND_POLL_MS * 3);
    expect(queueGets()).toBe(5);
  });
});

describe('3 блок возврата понятен', () => {
  it('заголовок «Агент вернул тебе: …», «Агент завис: …», «остановил владелец»', () => {
    const ts = new Date(2026, 9, 15, 19, 1).getTime();
    expect(returnedLine({ reason: 'нужно твоё слово', ts, by: 'agent' })).toBe('Агент вернул тебе: нужно твоё слово · 19:01');
    expect(returnedLine({ reason: 'завис: ни одного шага 5 мин', ts, by: 'system' })).toBe('Агент завис: ни одного шага 5 мин · 19:01');
    expect(returnedLine({ reason: 'остановил владелец', ts, by: 'ui' })).toBe('остановил владелец · 19:01');
  });

  it('подсказка в поле, обычная кнопка «Поручить агенту»; «Пусть решает» и «(необязательно)» больше нет', () => {
    const w = mount(ErrandReturned, {
      props: { returned: { reason: 'нужно твоё слово', ts: NOW, by: 'agent', detail: 'Что пробовал: прочитал ответ\nЧто нужно от тебя: скажи «накатывай»' }, testPrefix: 'x' },
    });
    expect(w.get('[data-test="x-returned"]').text()).toContain('Агент вернул тебе: нужно твоё слово');
    expect(w.get('[data-test="x-returned-detail"]').text()).toContain('Что нужно от тебя: скажи «накатывай»');
    const field = w.get('[data-test="x-say"]');
    expect(field.attributes('placeholder')).toBe('Скажи агенту, как решить — например: «накатывай»');
    const button = w.get('[data-test="x-let-decide"]');
    expect(button.text()).toBe('Поручить агенту');
    expect(button.classes()).not.toContain('owner-answer--main');
    expect(button.element.parentElement).toBe(field.element.parentElement); // поле и кнопка рядом
    expect(w.text()).not.toContain('Пусть решает');
    expect(w.text()).not.toContain('необязательно');
  });

  it('в карточке: текст -> POST /errand {text}; без текста — то же «Реши»', async () => {
    const item = (JSON.parse(JSON.stringify(FIXTURE)) as OwnerQueue).items.find((i) => i.ref === 'req:101')!;
    item.errand = null;
    item.errand_returned = { reason: 'нужно твоё слово', ts: NOW, by: 'agent', detail: null };
    const w = mount(OwnerCard, { props: { item, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    await w.get('[data-test="owner-say"]').setValue('накатывай');
    await w.get('[data-test="owner-let-decide"]').trigger('click');
    await flushPromises();
    const errands = sent.filter((s) => s.method === 'POST' && decodeURIComponent(s.path).endsWith('/errand'));
    expect(decodeURIComponent(errands[0]!.path)).toBe('/api/v1/owner_channel/req:101/errand');
    expect(errands[0]!.body).toEqual({ text: 'накатывай' });
    w.unmount();

    sent = [];
    const w2 = mount(OwnerCard, { props: { item, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    await w2.get('[data-test="owner-let-decide"]').trigger('click');
    await flushPromises();
    const again = sent.filter((s) => s.method === 'POST' && decodeURIComponent(s.path).endsWith('/errand'));
    expect(again[0]!.body).toEqual({ action: 'solve' });
    w2.unmount();
  });
});
