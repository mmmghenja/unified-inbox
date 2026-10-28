// Владелец 15.10.2026: карточка очереди «Агент», которую он смотрит или по которой пишет, не исчезает, когда ядро убрало
// её из очереди (сессия сделала новый ход и т. п.): остаётся с плашкой «Есть новее», набранный текст цел. Ход сессии
// по ушедшей карточке ещё можно ответить (ядро примет репликой), прочие — только скопировать и закрыть.
// Карточку, с которой не работали, очередь убирает как раньше.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OwnerItem, OwnerQueue } from '../src/api/types';
import AgentQueue from '../src/components/AgentQueue.vue';
import { store } from '../src/store/instance';
import { createOwnerState } from '../src/store/owner_channel';
import { closeWrite, writeModal } from '../src/store/writeModal';
import { ownerHold } from '../src/store/ownerHold';

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const NOW = 1760360000000;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function pick(ref: string): OwnerItem {
  return JSON.parse(JSON.stringify(FIXTURE.items.find((i) => i.ref === ref)!)) as OwnerItem;
}

let current: OwnerQueue;
let posted: string[] = [];
let wrapper: VueWrapper | null = null;

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  for (const key of Object.keys(ownerHold.cards)) delete ownerHold.cards[key];
  current = { ...FIXTURE, seq: 10, items: [pick('turn:11:4'), pick('draft:55')] };
  posted = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      if (url.pathname === '/api/v1/owner_channel/queue') return json(current);
      if (method === 'POST' && decodeURIComponent(url.pathname).endsWith('/answer')) {
        posted.push(decodeURIComponent(url.pathname));
        return json({ status: 'queued' });
      }
      if (url.pathname === '/api/v1/sync') return json({ messages: [], channels: [], max_seq: 1, more: false });
      return json({});
    }),
  );
});

afterEach(() => {
  closeWrite();
  wrapper?.unmount();
  wrapper = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

async function mountQueue(): Promise<VueWrapper> {
  wrapper = mount(AgentQueue, { props: { nowMs: NOW }, attachTo: document.body });
  await flushPromises();
  return wrapper;
}

/** Ядро вытеснило ход 4 ходом 5 той же сессии (и черновик снят) — очередь перечитана. */
async function nextTurn(): Promise<void> {
  const next = pick('turn:11:4');
  next.ref = 'turn:11:5';
  next.created_ts += 1000;
  current = { ...current, seq: 11, items: [next] };
  await store.loadOwner();
  await flushPromises();
}

function card(w: VueWrapper, ref: string) {
  return w.find(`.owner-card[data-ref="${ref}"]`);
}

describe('ушедшая из очереди карточка, с которой работают', () => {
  it('без работы карточка уходит вместе с очередью', async () => {
    const w = await mountQueue();
    expect(card(w, 'turn:11:4').exists()).toBe(true);
    await nextTurn();
    expect(card(w, 'turn:11:4').exists()).toBe(false);
    expect(card(w, 'draft:55').exists()).toBe(false);
    expect(card(w, 'turn:11:5').exists()).toBe(true);
  });

  it('начатый ответ по ходу остаётся с плашкой и уходит сессии', async () => {
    const w = await mountQueue();
    await card(w, 'turn:11:4').find('[data-action="reply"]').trigger('click');
    await card(w, 'turn:11:4').find('.owner-text-input').setValue('длинный ответ, почти дописал');
    await nextTurn();

    const old = card(w, 'turn:11:4');
    expect(old.exists()).toBe(true);
    expect(old.attributes('data-gone')).toBe('1');
    expect((old.find('.owner-text-input').element as HTMLTextAreaElement).value).toBe('длинный ответ, почти дописал');
    expect(old.find('[data-test="owner-gone"]').text()).toContain('Есть новее');
    expect(old.find('[data-test="owner-gone-new"]').exists()).toBe(true);
    expect(old.find('[data-test="owner-gone-copy"]').exists()).toBe(true);
    expect(card(w, 'turn:11:5').exists()).toBe(true);

    await old.find('[data-test="owner-text-send"]').trigger('click');
    await flushPromises();
    expect(posted).toEqual(['/api/v1/owner_channel/turn:11:4/answer']);
    expect(card(w, 'turn:11:4').exists()).toBe(false);
  });

  it('правленый черновик остаётся, ответить нельзя, «Закрыть» убирает', async () => {
    const w = await mountQueue();
    await card(w, 'draft:55').find('.owner-draft-edit').setValue('мой вариант');
    await nextTurn();

    const old = card(w, 'draft:55');
    expect(old.exists()).toBe(true);
    expect((old.find('.owner-draft-edit').element as HTMLTextAreaElement).value).toBe('мой вариант');
    expect(old.find('[data-action="approve"]').attributes('disabled')).toBeDefined();
    expect(old.find('[data-test="owner-gone"]').text()).toContain('ответить по ней нельзя');

    await old.find('[data-test="owner-gone-close"]').trigger('click');
    await flushPromises();
    expect(card(w, 'draft:55').exists()).toBe(false);
    expect(posted).toEqual([]);
  });

  it('открытое окно карточки не закрывается, когда она ушла из очереди', async () => {
    const turn = current.items[0]!;
    turn.full.answer = `**начало**\n${'длинный текст '.repeat(100)}`;
    const w = await mountQueue();
    await card(w, 'turn:11:4').find('.owner-open').trigger('click');
    await flushPromises();
    expect(document.body.querySelector('.owner-full-modal')).not.toBeNull();

    await nextTurn();
    expect(document.body.querySelector('.owner-full-modal')).not.toBeNull();
    expect(document.body.querySelector('.owner-full-modal [data-test="owner-gone"]')).not.toBeNull();

    (document.body.querySelector('.owner-full-modal [data-test="owner-gone-close"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.owner-full-modal')).toBeNull();
    expect(card(w, 'turn:11:4').exists()).toBe(false);
  });

  it('с карточки — в полный разговор с сессией: из карточки и из её окна', async () => {
    const turn = current.items[0]!;
    turn.full.answer = `**начало**\n${'длинный текст '.repeat(100)}`;
    const w = await mountQueue();
    await card(w, 'turn:11:4').find('[data-test="owner-open-session"]').trigger('click');
    expect(writeModal.open).toBe(true);
    expect(writeModal.target).toEqual({ kind: 'session', id: 11 });

    closeWrite();
    await card(w, 'turn:11:4').find('.owner-open').trigger('click');
    await flushPromises();
    (document.body.querySelector('[data-test="owner-modal-open-session"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.owner-full-modal')).toBeNull();
    expect(writeModal.target).toEqual({ kind: 'session', id: 11 });
  });

  it('у карточки без сессии ссылки нет', async () => {
    current = { ...current, items: [pick('agent_msg:31')] };
    const w = await mountQueue();
    expect(card(w, 'agent_msg:31').find('[data-test="owner-open-session"]').exists()).toBe(false);
  });

  it('«агент вернул» ведёт в разговор с агентом по этой карточке', async () => {
    const human = pick('human:7');
    human.errand = null;
    human.errand_returned = { ts: NOW - 60_000, reason: 'не дождался данных: password' };
    current = { ...current, items: [human] };
    const w = await mountQueue();
    await card(w, 'human:7').find('[data-test="owner-returned-talk"]').trigger('click');
    expect(writeModal.target).toMatchObject({ kind: 'owner_channel' });
    expect(writeModal.card?.ref).toBe('human:7');
  });
});
