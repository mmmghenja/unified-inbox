// Витрина, шаг 1 (`docs/architecture.md`): флаг сборки `VITE_SHOWCASE` (`util/showcase.ts`). Выключен —
// всё как было; включён — нет «Моего агента» (строка и разговор в «Написать…», «Написать…» в окне «Агент»),
// поручений агенту и его вопросов в карточках, «Моих данных», «Движка», образцов решений в «Автопилоте».
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HumanRequest, OwnerItem, OwnerQueue } from '../src/api/types';
import AgentQueue from '../src/components/AgentQueue.vue';
import AutopilotPanel from '../src/components/AutopilotPanel.vue';
import ErrandBlock from '../src/components/ErrandBlock.vue';
import HumanCard from '../src/components/HumanCard.vue';
import TopMenu from '../src/components/TopMenu.vue';
import WriteModalHost from '../src/components/WriteModalHost.vue';
import OwnerCard from '../src/components/OwnerCard.vue';
import { resetBoard } from '../src/store/board';
import { store } from '../src/store/instance';
import { createOwnerState } from '../src/store/owner_channel';
import { closeWrite, openWrite } from '../src/store/writeModal';
import { resetTalk } from '../src/store/ownerTalk';
import { layout } from '../src/util/layout';
import { isShowcase } from '../src/util/showcase';
import { firstWriteTarget, type WriteTargets } from '../src/util/writeTargets';

const NOW = 1760360000000;
const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const ERRAND = { id: 9, action: 'solve', label: 'Реши', text: null, created_ts: NOW - 20_000, progress: 'думаю…', progress_ts: NOW, auto: false };
const RETURNED = { reason: 'не вышло', ts: NOW - 1000, by: 'agent' as const, detail: null };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

// Разрешение из образца очереди + ряд «Поручить агенту» (у образца он только у заявки «нужен человек»).
function card(overrides: Partial<OwnerItem> = {}): OwnerItem {
  const base = FIXTURE.items.find((i) => i.ref === 'req:101')!;
  return {
    ...(JSON.parse(JSON.stringify(base)) as OwnerItem),
    errand: null,
    errand_returned: null,
    agent_ask: null,
    agent_actions: [{ id: 'solve', label: 'Реши', needs_text: false }],
    ...overrides,
  } as OwnerItem;
}

function human(overrides: Partial<HumanRequest> = {}): HumanRequest {
  return {
    id: 7, project_id: 5, kind: 'code', site: 'google.com', url: null, doing: 'регистрация', need: 'Введи код',
    profile: null, tab: null, shot: null, expires_ts: null, status: 'pending', decided_ts: null, decided_by: null,
    errand: null, errand_returned: null, role: 'менеджер', role_no: 1, created_ts: Date.now(), seq: 1,
    ...overrides,
  };
}

let queue: OwnerItem[] = [];

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetTalk();
  closeWrite();
  layout.mode = 'full';
  store.state.owner_channel = createOwnerState();
  queue = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const p = new URL(input, 'http://localhost').pathname;
      if (p === '/api/v1/owner_channel/queue') return json({ items: queue, seq: 1, agent: { online: true, seen_ts: NOW } });
      if (p === '/api/v1/owner_channel/talk') return json({ items: [], has_more: false });
      if (p === '/api/v1/subs') return json({ items: [], lines: [] });
      if (p === '/api/v1/sessions') return json({ items: [] });
      return json([]);
    }),
  );
});

afterEach(() => {
  closeWrite();
  resetTalk();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

const MODES = [
  { name: 'флаг выключен', env: '', on: false },
  { name: 'VITE_SHOWCASE=1', env: '1', on: true },
] as const;

describe('isShowcase', () => {
  it.each([
    ['', false],
    ['0', false],
    ['false', false],
    ['1', true],
    ['true', true],
    [' YES ', true],
  ])('VITE_SHOWCASE=%j -> %s', (value, expected) => {
    vi.stubEnv('VITE_SHOWCASE', value);
    expect(isShowcase()).toBe(expected);
  });
});

describe.each(MODES)('витрина: $name', ({ env, on }) => {
  beforeEach(() => {
    vi.stubEnv('VITE_SHOWCASE', env);
  });

  it('меню «⋯»: «Мои данные» и «Движок»', async () => {
    const w = mount(TopMenu, { attachTo: document.body });
    await w.get('.top-menu-button').trigger('click');
    expect(w.find('[data-test="my-data-open"]').exists()).toBe(!on);
    expect(w.find('[data-testid="engine-chip"]').exists()).toBe(!on);
    expect(w.find('.autopilot-open').exists()).toBe(true);
    w.unmount();
  });

  it('«Написать…»: строка и разговор «Мой агент»; прямое открытие на агенте', async () => {
    openWrite();
    const host = mount(WriteModalHost, { attachTo: document.body });
    await flushPromises();
    expect(document.body.querySelector('.write-modal')).not.toBeNull();
    expect(document.body.querySelector('[data-write="owner_channel"]') !== null).toBe(!on);
    expect(document.body.querySelector('.owner-talk') !== null).toBe(!on);
    host.unmount();
    closeWrite();
    openWrite({ kind: 'owner_channel' });
    const again = mount(WriteModalHost, { attachTo: document.body });
    await flushPromises();
    expect(document.body.querySelector('.owner-talk') !== null).toBe(!on);
    again.unmount();
  });

  it('Enter в поиске «Написать…» не ведёт к агенту', () => {
    const row = { target: { kind: 'session' as const, id: 5 }, name: 'программист', sub: '', dot: 'idle' as const, unread: 0, offline: false };
    const targets: WriteTargets = {
      owner_channel: { target: { kind: 'owner_channel' }, name: 'Мой агент', sub: 'на связи', dot: 'run', unread: 0, offline: false },
      groups: [{ id: 3, name: 'Umbrella', rows: [row] }],
    };
    expect(firstWriteTarget(targets, '')).toEqual(on ? { kind: 'session', id: 5 } : { kind: 'owner_channel' });
  });

  it('окно «Агент»: кнопка «Написать…»', async () => {
    const q = mount(AgentQueue, { attachTo: document.body });
    await flushPromises();
    expect(q.find('[data-test="agent-write"]').exists()).toBe(!on);
    expect(q.find('[data-test="agent-terminal"]').exists()).toBe(true);
    q.unmount();
  });

  it('карточка очереди: «Поручить агенту», ход поручения, вопрос агента, «обсудить с агентом»', async () => {
    const free = mount(OwnerCard, { props: { item: card(), nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(document.body.querySelector('.owner-agent-actions') !== null).toBe(!on);
    expect(free.find('[data-test="owner-discuss"]').exists()).toBe(!on);
    expect(document.body.textContent).toContain('Разрешить');
    free.unmount();

    const busy = mount(OwnerCard, {
      props: { item: card({ errand: ERRAND, agent_ask: { id: 1, text: 'какой телефон?', ts: NOW, until_ts: null } } as Partial<OwnerItem>), nowMs: NOW, agentOnline: true },
      attachTo: document.body,
    });
    expect(busy.find('[data-test="owner-errand"]').exists()).toBe(!on);
    expect(busy.find('[data-test="owner-ask"]').exists()).toBe(!on);
    busy.unmount();

    const back = mount(OwnerCard, { props: { item: card({ errand_returned: RETURNED }), nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(back.find('[data-test="owner-returned"]').exists()).toBe(!on);
    back.unmount();
  });

  it('заявка «нужен человек»: ход поручения и возврат', () => {
    const busy = mount(HumanCard, { props: { human: human({ errand: ERRAND } as Partial<HumanRequest>) } });
    expect(busy.find('[data-test="human-errand"]').exists()).toBe(!on);
    busy.unmount();
    const back = mount(HumanCard, { props: { human: human({ errand_returned: RETURNED }) } });
    expect(back.find('.human-returned').exists()).toBe(!on);
    back.unmount();
  });

  it('карточки групп и «выйти»: блок поручения', () => {
    store.state.owner_channel.items = [card({ ref: 'leave:3', errand: ERRAND })];
    const w = mount(ErrandBlock, { props: { itemRef: 'leave:3', title: 'выйти', testPrefix: 'leave' } });
    expect(w.find('[data-test="leave-errand"]').exists()).toBe(!on);
    w.unmount();
  });

  it('«Автопилот»: раздел образцов решений', async () => {
    const w = mount(AutopilotPanel, { global: { stubs: { AgentPatternsSection: { template: '<div class="patterns-stub" />' } } } });
    await flushPromises();
    expect(w.find('.patterns-stub').exists()).toBe(!on);
    w.unmount();
  });
});
