// Ф58-3 (`docs/architecture.md`–3): «Реши» доводит дело до результата. Пока по карточке
// открыто поручение агенту — ни кнопок ответа владельца, ни «Поручить агенту»: строка «решает агент: <действие> ·
// <прогресс> · N мин» и «Забрать у агента». Агент вернул — строка «агент вернул: <причина> · ЧЧ:ММ» и кнопки.
// Карточка очереди (`OwnerCard`) и заявка «нужен человек» (`HumanCard`: главная, «Ждёт тебя», телефон — один
// компонент); 409 `errand_open` — словами, а не «уже решено».
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HumanRequest, Project, OwnerItem, OwnerQueue } from '../src/api/types';
import HumanCard from '../src/components/HumanCard.vue';
import OwnerCard from '../src/components/OwnerCard.vue';
import { store } from '../src/store/instance';
import { createHumanState } from '../src/store/humanRequests';
import { createOwnerState } from '../src/store/owner_channel';
import { closeWrite, writeModal } from '../src/store/writeModal';
import { errandLine, humanErrandsMark, returnedLine } from '../src/util/owner_channel';

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const NOW = 1760360000000;
const MIN = 60000;

function item(ref: string): OwnerItem {
  const found = (JSON.parse(JSON.stringify(FIXTURE)) as OwnerQueue).items.find((i) => i.ref === ref);
  if (found === undefined) throw new Error(`нет карточки ${ref} в фикстуре`);
  return found;
}

function human(overrides: Partial<HumanRequest> = {}): HumanRequest {
  return {
    id: 7,
    project_id: 5,
    kind: 'code',
    site: 'google.com',
    url: null,
    doing: 'регистрация',
    need: 'Введи код из письма',
    profile: null,
    tab: null,
    shot: null,
    expires_ts: null,
    status: 'pending',
    decided_ts: null,
    decided_by: null,
    errand: null,
    errand_returned: null,
    role: 'менеджер',
    role_no: 1,
    created_ts: Date.now(),
    seq: 1,
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
}
let sent: Sent[] = [];
let handler: (s: Sent) => Response | undefined;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  store.state.humans = createHumanState();
  store.state.projects = [{ id: 5, name: 'Hooli', color: '#0a0', sort: 0, muted: false, workdir: null, unread: 0 } as Project];
  sent = [];
  handler = () => undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname };
      sent.push(s);
      const custom = handler(s);
      if (custom !== undefined) return custom;
      if (s.path === '/api/v1/owner_channel/queue') return json({ items: [], seq: 1, agent: { online: true, seen_ts: NOW } });
      if (s.path === '/api/v1/human-requests') return json([]);
      if (s.path === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      return json({ ok: true });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('58-3 util', () => {
  it('строка поручения: действие, прогресс, минуты от создания; без действия — текст', () => {
    const errand = { id: 1, action: 'solve', label: 'Реши', text: null, created_ts: NOW - 4 * MIN, progress: 'ввожу код' };
    expect(errandLine(errand, NOW)).toBe('решает агент: Реши · ввожу код · 4 мин');
    expect(errandLine({ ...errand, label: null, text: 'сделай сам', progress: null }, NOW)).toBe('решает агент: сделай сам · 4 мин');
    expect(errandLine(null, NOW)).toBeNull();
    expect(errandLine(undefined, NOW)).toBeNull();
  });

  it('строка возврата: причина и ЧЧ:ММ; нет — null', () => {
    const ts = new Date(2026, 9, 14, 17, 34).getTime();
    expect(returnedLine({ reason: 'письмо не пришло', ts })).toBe('Агент вернул тебе: письмо не пришло · 17:34');
    expect(returnedLine(null)).toBeNull();
    expect(returnedLine(undefined)).toBeNull();
  });

  it('отпечаток поручений по заявкам', () => {
    const h = item('human:7');
    const before = humanErrandsMark([h, item('req:104')]);
    expect(before).not.toContain('req:104'); // только заявки «нужен человек»
    h.errand = { id: 5, action: null, label: null, text: 'реши', created_ts: NOW, progress: null };
    expect(humanErrandsMark([h])).not.toBe(before);
  });
});

describe('58-3 карточка очереди', () => {
  it('поручение открыто: строка «решает агент» и кнопки ответа на месте (Ф65-1), ряда агента нет; «Остановить агента» -> DELETE', async () => {
    const it104 = item('req:104');
    it104.agent_actions = [{ id: 'solve', label: 'Реши', needs_text: false }];
    expect(it104.answers.length).toBeGreaterThan(0);
    const w = mount(OwnerCard, { props: { item: it104, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(w.get('[data-test="owner-errand"]').text()).toContain('решает агент: Разрешить за меня · проверяю · 1 мин');
    expect(w.find('.owner-actions').exists()).toBe(true);
    expect(w.find('[data-action="allow"]').exists()).toBe(true);
    expect(w.find('.owner-agent-actions').exists()).toBe(false);
    expect(w.find('[data-test="owner-returned"]').exists()).toBe(false);
    const take = w.get('[data-test="owner-take-back"]');
    expect(take.text()).toBe('Остановить агента');
    await take.trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'DELETE' && s.path === '/api/v1/owner_channel/errands/77')).toBe(true);
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/human-requests')).toBe(true);
    w.unmount();
  });

  it('агент вернул: строка «агент вернул …» и кнопки ответа на месте', async () => {
    const card = item('req:101');
    card.errand = null;
    card.errand_returned = { reason: 'нет доступа', ts: new Date(2026, 9, 14, 9, 5).getTime() };
    card.agent_actions = [{ id: 'solve', label: 'Реши', needs_text: false }];
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(w.get('[data-test="owner-returned"]').text()).toBe('Агент вернул тебе: нет доступа · 09:05');
    expect(w.find('[data-test="owner-errand"]').exists()).toBe(false);
    expect(w.find('[data-action="allow"]').exists()).toBe(true);
    expect(w.find('.owner-agent-actions').exists()).toBe(true);
    w.unmount();
  });
});

describe('58-3 заявка «нужен человек» (главная, «Ждёт тебя», телефон)', () => {
  it('поручение открыто: строка и «Остановить агента» -> DELETE и перечитать заявки; «Готово / Не вышло / Не сейчас» на месте', async () => {
    const h = human({
      errand: { id: 362, action: 'solve', label: 'Реши', text: null, created_ts: Date.now() - 3 * MIN, progress: 'жду письмо' },
    });
    const w = mount(HumanCard, { props: { human: h } });
    expect(w.findAll('.human-actions button').map((b) => b.text())).toEqual(['Готово', 'Не вышло', 'Не сейчас']);
    expect(w.get('[data-test="human-errand"]').text()).toContain('решает агент: Реши · жду письмо · 3 мин');
    await w.get('[data-test="human-take-back"]').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'DELETE' && s.path === '/api/v1/owner_channel/errands/362')).toBe(true);
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/human-requests')).toBe(true);
  });

  it('агент вернул — строка «агент вернул …» и кнопки снова', async () => {
    const h = human({ errand_returned: { reason: 'код не пришёл', ts: new Date(2026, 9, 14, 17, 40).getTime() } });
    const w = mount(HumanCard, { props: { human: h } });
    expect(w.get('[data-test="human-returned"]').text()).toBe('Агент вернул тебе: код не пришёл · 17:40');
    // Владелец 15.10.2026: из строки — в разговор с агентом по этой заявке
    await w.get('[data-test="human-returned-talk"]').trigger('click');
    expect(writeModal.target).toEqual({ kind: 'owner_channel' });
    expect(writeModal.card?.ref).toMatch(/^human:\d+$/);
    closeWrite();
    expect(w.findAll('.human-actions button').map((b) => b.text())).toEqual(['Готово', 'Не вышло', 'Не сейчас']);
    expect(w.find('[data-test="human-errand"]').exists()).toBe(false);
  });

  it('без поручения (и старое ядро без полей) — как раньше', () => {
    const h = human();
    delete (h as Partial<HumanRequest>).errand;
    delete (h as Partial<HumanRequest>).errand_returned;
    const w = mount(HumanCard, { props: { human: h } });
    expect(w.find('.human-actions').exists()).toBe(true);
    expect(w.find('[data-test="human-errand"]').exists()).toBe(false);
    expect(w.find('[data-test="human-returned"]').exists()).toBe(false);
  });

  it('очередь принесла новое поручение по заявке — заявки перечитываются (строка появится без ожидания опроса)', async () => {
    const h = item('human:7');
    h.errand = null;
    let items: OwnerItem[] = [h];
    handler = (s) => (s.path === '/api/v1/owner_channel/queue' ? json({ items, seq: 2, agent: { online: true, seen_ts: NOW } }) : undefined);
    await store.loadOwner();
    await flushPromises();
    const reads = () => sent.filter((s) => s.path === '/api/v1/human-requests').length;
    const first = reads();
    await store.loadOwner();
    await flushPromises();
    expect(reads()).toBe(first); // ничего не изменилось
    items = [{ ...h, errand: { id: 9, action: null, label: null, text: 'реши', created_ts: NOW, progress: null } }];
    await store.loadOwner();
    await flushPromises();
    expect(reads()).toBe(first + 1);
  });
});
