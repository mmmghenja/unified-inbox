// Ф56-3 (`docs/architecture.md`): окно «Агент» — очередь владельца. Порядок, метки, brief/full (внутри
// карточки и окном), кнопки из `answers`, текст под карточкой, вопросы, правка черновика, 409 «уже решено»,
// поручения агенту, пустое состояние, чипы фильтра, чужой текст — не HTML.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OwnerItem, OwnerQueue } from '../src/api/types';
import AgentQueue from '../src/components/AgentQueue.vue';
import OwnerCard from '../src/components/OwnerCard.vue';
import { store } from '../src/store/instance';
import { applyOwnerQueue, createOwnerState } from '../src/store/owner_channel';
import { filterOwner, kindLabel, passedLine, pathLine, sortOwner } from '../src/util/owner_channel';

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const NOW = 1760360000000;
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function queue(): OwnerQueue {
  return JSON.parse(JSON.stringify(FIXTURE)) as OwnerQueue;
}

function item(ref: string): OwnerItem {
  const found = queue().items.find((i) => i.ref === ref);
  if (found === undefined) throw new Error(`нет карточки ${ref} в фикстуре`);
  return found;
}

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
let handler: (s: Sent) => Response | undefined;

function posts(suffix: string): Sent[] {
  return sent.filter((s) => s.method === 'POST' && decodeURIComponent(s.path).endsWith(suffix));
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  current = queue();
  sent = [];
  handler = () => undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      sent.push(s);
      const custom = handler(s);
      if (custom !== undefined) return custom;
      if (s.path === '/api/v1/owner_channel/queue') return json(current);
      if (s.path === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      return json({ ref: 'x', status: 'ok' });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete (window as unknown as { __xss?: number }).__xss;
});

async function mountQueue() {
  const w = mount(AgentQueue, { props: { nowMs: NOW }, attachTo: document.body });
  await flushPromises();
  return w;
}

function card(w: ReturnType<typeof mount>, ref: string) {
  return w.get(`[data-ref="${ref}"]`);
}

describe('56-3 util', () => {
  it('порядок: срочное, человек, разрешения и вопросы, остальное; внутри старые выше', () => {
    const order = sortOwner(queue().items, NOW).map((i) => i.ref);
    expect(order[0]).toBe('human:7'); // срок через 8 минут — срочное
    expect(order.indexOf('human:7')).toBeLessThan(order.indexOf('req:101'));
    // разрешения/вопросы (по возрасту: 101 старше 102 и 104) раньше черновика
    expect(order.indexOf('req:101')).toBeLessThan(order.indexOf('req:102'));
    expect(order.indexOf('req:102')).toBeLessThan(order.indexOf('draft:55'));
    // «остальное» — по created_ts: старый stall (40 мин) раньше свежего turn (8 мин)
    expect(order.indexOf('stall:11:1')).toBeLessThan(order.indexOf('turn:11:4'));
    // срок press через 30 минут — не срочное
    expect(order.indexOf('press:9')).toBeGreaterThan(order.indexOf('req:104'));
  });

  it('«нужен человек» без срока идёт раньше разрешений и вопросов, те — раньше остального', () => {
    const human = item('human:7');
    human.until_ts = null;
    human.created_ts = NOW; // самый свежий — всё равно первым
    const order = sortOwner([item('draft:55'), item('req:101'), human], NOW).map((i) => i.ref);
    expect(order).toEqual(['human:7', 'req:101', 'draft:55']);
  });

  it('пометки и путь: словами, пустые звенья пропускаются, about — «о <роль>»', () => {
    expect(kindLabel('agent_msg')).toBe('сообщение агента');
    expect(kindLabel('stuck')).toBe('завис');
    expect(kindLabel('что-то_новое')).toBe('что-то_новое');
    expect(pathLine(item('agent_msg:31'))).toBe('Umbrella › оркестратор › маркетолог · о секретарь');
    const noHead = item('req:101');
    noHead.path = { project: 'П', head: null, author: 'Р' };
    expect(pathLine(noHead)).toBe('П › Р');
    expect(passedLine(item('req:101'))).toBe('передал менеджер: не уверен, можно ли пушить');
    expect(passedLine(item('req:102'))).toBeNull();
  });

  it('фильтр: «в канале» и «только мне» делят очередь по in_channel', () => {
    const all = queue().items;
    const inCh = filterOwner(all, 'channel');
    const mine = filterOwner(all, 'mine');
    expect(inCh.every((i) => i.in_channel)).toBe(true);
    expect(inCh.length + mine.length).toBe(all.length);
    expect(filterOwner(all, 'all')).toHaveLength(all.length);
  });

  it('снимок старее применённого не откатывает очередь', () => {
    const state = createOwnerState();
    applyOwnerQueue(state, { ...queue(), seq: 10 });
    applyOwnerQueue(state, { items: [], seq: 5, agent: { online: false, seen_ts: null } });
    expect(state.items.length).toBeGreaterThan(0);
    expect(state.seq).toBe(10);
  });
});

describe('56-3 окно', () => {
  it('шапка: состояние агента, счётчик; карточки по порядку; пометки «у агента», «передал», «решает агент»', async () => {
    const w = await mountQueue();
    expect(w.get('[data-test="agent-state"]').text()).toBe('агент на связи');
    expect(w.get('[data-test="agent-count"]').text()).toBe(String(FIXTURE.items.length));
    const refs = w.findAll('.owner-card').map((c) => c.attributes('data-ref'));
    expect(refs[0]).toBe('human:7');
    expect(card(w, 'human:7').text()).toContain('у агента');
    expect(card(w, 'req:101').get('[data-test="owner-passed"]').text()).toBe('передал менеджер: не уверен, можно ли пушить');
    // Ф58-3: «решает агент: <действие> · <прогресс> · N мин» (время — от создания поручения)
    expect(card(w, 'req:104').get('[data-test="owner-errand"]').text()).toContain('решает агент: Разрешить за меня · проверяю · 1 мин');
    expect(card(w, 'human:7').find('.owner-until--urgent').exists()).toBe(true);
    expect(card(w, 'turn:11:4').text()).toContain('ответ сессии');
    w.unmount();
  });

  it('агент не на связи — «не на связи», пометка «в канале» вместо «у агента»', async () => {
    current.agent = { online: false, seen_ts: null };
    const w = await mountQueue();
    expect(w.get('[data-test="agent-state"]').text()).toBe('агент не на связи');
    expect(card(w, 'human:7').text()).toContain('в канале');
    expect(card(w, 'human:7').text()).not.toContain('у агента');
    w.unmount();
  });

  it('чипы фильтра: «в канале» оставляет только in_channel', async () => {
    const w = await mountQueue();
    await w.get('[data-filter="channel"]').trigger('click');
    const refs = w.findAll('.owner-card').map((c) => c.attributes('data-ref'));
    expect(refs.sort()).toEqual(FIXTURE.items.filter((i) => i.in_channel).map((i) => i.ref).sort());
    await w.get('[data-filter="mine"]').trigger('click');
    expect(w.findAll('.owner-card')).toHaveLength(FIXTURE.items.filter((i) => !i.in_channel).length);
    w.unmount();
  });

  it('пусто: «Ничего не ждёт» и состояние агента', async () => {
    current = { items: [], seq: 3, agent: { online: false, seen_ts: null } };
    const w = await mountQueue();
    expect(w.get('[data-test="agent-empty"]').text()).toBe('Ничего не ждёт');
    expect(w.get('[data-test="agent-state"]').text()).toBe('агент не на связи');
    w.unmount();
  });

  it('brief — кратко; короткое full раскрывается внутри карточки, длинное — окном поверх (Esc закрывает)', async () => {
    const long = item('turn:11:4');
    long.full.answer = `**начало**\n${'длинный текст '.repeat(100)}`;
    current.items = [item('req:101'), long];
    const w = await mountQueue();
    const perm = card(w, 'req:101');
    expect(perm.find('.owner-full').exists()).toBe(false);
    await perm.get('.owner-expand').trigger('click');
    // Команда целиком видна в `brief` — под «развернуть» только то, чего в строке нет.
    expect(perm.get('.owner-full').text()).toBe('причина: публикация');
    const turn = card(w, 'turn:11:4');
    await turn.get('.owner-open').trigger('click');
    expect(turn.find('.owner-full').exists()).toBe(false);
    const modal = document.body.querySelector('.owner-full-modal');
    expect(modal).not.toBeNull();
    expect(modal!.querySelector('strong')!.textContent).toBe('начало'); // текст модели — markdown
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(document.body.querySelector('.owner-full-modal')).toBeNull();
    w.unmount();
  });

  it('кнопки — из answers в их порядке и с их подписями; клик шлёт action', async () => {
    const w = await mountQueue();
    const perm = card(w, 'req:101');
    expect(perm.findAll('.owner-answer').map((b) => b.text())).toEqual(['Разрешить', 'Отказать']);
    await perm.get('[data-action="allow"]').trigger('click');
    await flushPromises();
    expect(posts('/answer')).toHaveLength(1);
    expect(decodeURIComponent(posts('/answer')[0]!.path)).toBe('/api/v1/owner_channel/req:101/answer');
    expect(posts('/answer')[0]!.body).toEqual({ action: 'allow' });
    w.unmount();
  });

  it('needs_text: кнопка открывает поле, тело содержит text; «Отмена» закрывает; пустой текст не отправить', async () => {
    const w = await mountQueue();
    const turn = card(w, 'turn:11:4');
    await turn.get('[data-action="reply"]').trigger('click');
    const send = turn.get('[data-test="owner-text-send"]');
    expect(send.attributes('disabled')).toBeDefined();
    await turn.get('textarea.owner-text-input').setValue('сделай ещё X');
    await send.trigger('click');
    await flushPromises();
    expect(posts('/answer')[0]!.body).toEqual({ action: 'reply', text: 'сделай ещё X' });
    await card(w, 'agent_msg:31').get('[data-action="reply"]').trigger('click');
    expect(card(w, 'agent_msg:31').find('textarea').exists()).toBe(true);
    await card(w, 'agent_msg:31').findAll('button').find((b) => b.text() === 'Отмена')!.trigger('click');
    expect(card(w, 'agent_msg:31').find('textarea').exists()).toBe(false);
    w.unmount();
  });

  it('needs_text: повторное нажатие верхней кнопки не стирает набранное (жалоба владельца)', async () => {
    const w = await mountQueue();
    const turn = card(w, 'turn:11:4');
    await turn.get('[data-action="reply"]').trigger('click');
    await turn.get('textarea.owner-text-input').setValue('берём, куплю');
    await turn.get('[data-action="reply"]').trigger('click');
    expect((turn.get('textarea.owner-text-input').element as HTMLTextAreaElement).value).toBe('берём, куплю');
    await turn.get('[data-test="owner-text-send"]').trigger('click');
    await flushPromises();
    expect(posts('/answer')[0]!.body).toEqual({ action: 'reply', text: 'берём, куплю' });
    w.unmount();
  });

  it('question: «Ответить» активна, когда отвечены все; тело — answers {вопрос: ответ}', async () => {
    const w = await mountQueue();
    const q = card(w, 'req:102');
    const answer = q.get('[data-action="answer"]');
    expect(answer.attributes('disabled')).toBeDefined();
    await q.findAll('.work-question-option').find((b) => b.text() === 'Про')!.trigger('click');
    expect(answer.attributes('disabled')).toBeUndefined();
    await answer.trigger('click');
    await flushPromises();
    expect(posts('/answer')[0]!.body).toEqual({ action: 'answer', answers: { 'Какой тариф выбрать?': 'Про' } });
    w.unmount();
  });

  it('draft: правка текста уходит в approve; без правки text нет; «Отправить сейчас» — send_now', async () => {
    const w = await mountQueue();
    const d = card(w, 'draft:55');
    expect((d.get('textarea.owner-draft-edit').element as HTMLTextAreaElement).value).toBe('Добрый день! Высылаю счёт.');
    await d.get('[data-action="approve"]').trigger('click');
    await flushPromises();
    expect(posts('/answer')[0]!.body).toEqual({ action: 'approve' });
    await d.get('textarea.owner-draft-edit').setValue('Добрый день! Счёт во вложении.');
    await d.get('[data-action="send_now"]').trigger('click');
    await flushPromises();
    expect(posts('/answer')[1]!.body).toEqual({ action: 'approve', send_now: true, text: 'Добрый день! Счёт во вложении.' });
    w.unmount();
  });

  it('нужен человек: поля «где и что сделать», url — текстом с «копировать», скриншот миниатюрой и окном', async () => {
    const w = await mountQueue();
    const h = card(w, 'human:7');
    expect(h.text()).toContain('jobs.example');
    expect(h.text()).toContain('work');
    expect(h.text()).toContain('Вкладка 2');
    expect(h.get('.human-url-text').text()).toBe('https://jobs.example/login?x=1');
    expect(h.find('a').exists()).toBe(false);
    expect(h.findAll('.owner-answer').map((b) => b.text())).toEqual(['Готово', 'Не вышло', 'Не сейчас']);
    expect(h.get('.human-shot img').attributes('src')).toBe('/api/v1/human-requests/7/shot');
    await h.get('.human-shot').trigger('click');
    expect(document.body.querySelector('.human-shot-modal')).not.toBeNull();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    w.unmount();
  });

  it('409 — «уже решено»: очередь перечитана, карточки нет, в шапке пометка', async () => {
    handler = (s) => {
      if (s.method === 'POST' && s.path.includes('/answer')) {
        current = { ...current, items: current.items.filter((i) => i.ref !== 'req:101') };
        return json({ detail: 'уже решена' }, 409);
      }
      return undefined;
    };
    const w = await mountQueue();
    const reads = sent.filter((s) => s.path === '/api/v1/owner_channel/queue').length;
    await card(w, 'req:101').get('[data-action="allow"]').trigger('click');
    await flushPromises();
    expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue').length).toBeGreaterThan(reads);
    expect(w.find('[data-ref="req:101"]').exists()).toBe(false);
    expect(w.get('[data-test="agent-notice"]').text()).toContain('Уже решено');
    w.unmount();
  });

  it('другая ошибка — строка под карточкой, карточка остаётся', async () => {
    handler = (s) => (s.method === 'POST' && s.path.includes('/answer') ? json({ detail: 'boom' }, 500) : undefined);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const w = await mountQueue();
    await card(w, 'req:101').get('[data-action="allow"]').trigger('click');
    await flushPromises();
    expect(card(w, 'req:101').get('[role="alert"]').text()).toContain('Не отправилось');
    w.unmount();
  });

  it('кнопки агента: без текста — сразу errand; с текстом — поле; поручение открыто — ряд заменён строкой и «Остановить агента»', async () => {
    const w = await mountQueue();
    const h = card(w, 'human:7');
    expect(h.get('.owner-agent-label').text()).toBe('Поручить агенту:');
    await h.get('[data-agent-action="solve"]').trigger('click');
    await flushPromises();
    expect(decodeURIComponent(posts('/errand')[0]!.path)).toBe('/api/v1/owner_channel/human:7/errand');
    expect(posts('/errand')[0]!.body).toEqual({ action: 'solve' });
    await h.get('[data-agent-action="note"]').trigger('click');
    await h.get('textarea.owner-text-input').setValue('вот заметка');
    await h.get('[data-test="owner-text-send"]').trigger('click');
    await flushPromises();
    expect(posts('/errand')[1]!.body).toEqual({ action: 'note', text: 'вот заметка' });
    // открытое поручение: нет ряда, есть «Забрать у агента» -> DELETE
    const open = card(w, 'req:104');
    expect(open.find('.owner-agent-actions').exists()).toBe(false);
    await open.get('[data-test="owner-errand"] button').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'DELETE' && s.path === '/api/v1/owner_channel/errands/77')).toBe(true);
    w.unmount();
  });

  it('чужой текст — только текстом (brief, full, путь, роль)', async () => {
    const evil = item('agent_msg:31');
    evil.brief = XSS;
    evil.full.text = XSS;
    evil.path.author = XSS;
    current.items = [evil, { ...item('req:101'), brief: XSS, full: { ...item('req:101').full, input_text: XSS } }];
    const w = await mountQueue();
    await w.get('[data-ref="req:101"] .owner-expand').trigger('click');
    expect(w.find('img').exists()).toBe(false);
    expect(w.html()).not.toContain('<script');
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
    expect(w.get('[data-ref="req:101"] .owner-brief').text()).toContain('<img');
    w.unmount();
  });

  it('состояния сессии: подписанные поля вместо сырых ключей', async () => {
    const w = mount(OwnerCard, { props: { item: item('stuck:11:1'), nowMs: NOW, agentOnline: true } });
    await w.get('.owner-expand').trigger('click');
    const text = w.get('.owner-full').text();
    expect(text).toContain('не возвращается: 30 мин назад');
    expect(text).toContain('ход начат: 35 мин назад');
    expect(text).not.toMatch(/since_ts|origin|state|1760\d{9}/);
    expect(w.findAll('.owner-answer').map((b) => b.text())).toEqual(['Стоп', 'Прервать и сказать…', 'Пусть работает']);
    w.unmount();
  });

  it('Ф93-1: денежная карточка — пометка «💰 деньги» рядом с видом; у прочих её нет', async () => {
    const money = mount(OwnerCard, { props: { item: { ...item('agent_msg:31'), money: true }, nowMs: NOW, agentOnline: true } });
    expect(money.get('[data-test="owner-money"]').text()).toBe('💰 деньги');
    money.unmount();
    const plain = mount(OwnerCard, { props: { item: item('agent_msg:31'), nowMs: NOW, agentOnline: true } });
    expect(plain.find('[data-test="owner-money"]').exists()).toBe(false);
    plain.unmount();
  });
});

describe('56-3 стор', () => {
  it('по /sync перечитывает, если max_seq ушёл дальше снимка (не чаще раза в 2 с); раньше первой загрузки — нет', async () => {
    vi.useFakeTimers();
    try {
      const priv = store as unknown as { onOwnerSeq(n: number): void };
      priv.onOwnerSeq(900);
      await vi.advanceTimersByTimeAsync(10);
      expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue')).toHaveLength(0);
      await store.loadOwner(); // seq 500
      expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue')).toHaveLength(1);
      priv.onOwnerSeq(400); // не вырос
      priv.onOwnerSeq(900); // вырос, но только что грузили — отложено
      priv.onOwnerSeq(901);
      expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue')).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(2100);
      expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue')).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('watchOwner: грузит сразу и раз в минуту, пока окно открыто', async () => {
    vi.useFakeTimers();
    try {
      const stop = store.watchOwner();
      await vi.advanceTimersByTimeAsync(10);
      expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue')).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(60_100);
      expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue')).toHaveLength(2);
      stop();
      await vi.advanceTimersByTimeAsync(120_000);
      expect(sent.filter((s) => s.path === '/api/v1/owner_channel/queue')).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
