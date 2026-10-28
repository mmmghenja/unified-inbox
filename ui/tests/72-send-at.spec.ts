// Ф30-1 C (`docs/architecture.md` и поправка П3): черновик сессии «к точному
// времени» — пометка «к 10:00, 5 окт»; подтверждённый — «уйдёт в …» и «Отменить»; 409 `send_at_passed` на
// «Отправить» (одиночный и в пачке) — «время прошло» и «Отправить сейчас» (`approve` с `send_now: true`);
// `failed` по опозданию — причина простыми словами.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OutboxListItem, Project } from '../src/api/types';
import DraftBatchBar from '../src/components/DraftBatchBar.vue';
import DraftCard from '../src/components/DraftCard.vue';
import DraftRow from '../src/components/DraftRow.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import { store } from '../src/store/instance';
import { buildBoard } from '../src/util/board';
import { formatTime, startOfDay } from '../src/util/home';
import { batchErrors, batchPassed, outboxErrorText, sendAtLabel } from '../src/util/pace';

const NOW = new Date(2026, 9, 2, 9, 0, 0).getTime();
const AT = new Date(2026, 9, 5, 10, 0, 0).getTime();
const PASSED = 'время отправки 10:00 прошло';

function project(): Project {
  return { id: 3, name: 'Umbrella', color: null, sort: 0, muted: false, workdir: null, unread: 0 };
}

function draft(id: number, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: 100 + id,
    project_id: 3,
    channel_title: 'Вебинар',
    text: `Пост ${id}`,
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: NOW - 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Umbrella',
    source: 'telegram',
    kind: 'group',
    author: 'mcp:iip',
    reply_to: null,
    thread: null,
    paced: false,
    not_before_ts: null,
    send_at_ts: AT,
    account_id: 1,
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Recorded {
  method: string;
  path: string;
  body: unknown;
}

/** `/approve` без `send_now` — 409 `send_at_passed`; с `send_now` — 200. */
function stubFetch(extra: (path: string, body: unknown) => Response | undefined = () => undefined): Recorded[] {
  const calls: Recorded[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null;
      calls.push({ method, path: url.pathname, body });
      const custom = extra(url.pathname, body);
      if (custom) return custom;
      if (url.pathname.endsWith('/approve')) {
        if ((body as { send_now?: boolean }).send_now === true) {
          return json({ status: 'queued', paced: false, not_before_ts: null });
        }
        return json({ detail: PASSED, error: 'send_at_passed', send_at_ts: AT }, 409);
      }
      if (url.pathname.endsWith('/cancel')) return json({ status: 'cancelled' });
      if (url.pathname === '/api/v1/sync') return json({ changes: [], max_seq: 0 });
      if (/^\/api\/v1\/outbox\/\d+$/.test(url.pathname)) return json({ error: 'send_at_late' });
      return json([]);
    }),
  );
  return calls;
}

const approves = (calls: Recorded[]) => calls.filter((c) => c.method === 'POST' && c.path.endsWith('/approve'));

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.draftSelection = [];
  store.state.homeOpen = false;
  store.state.projects = [project()];
  store.state.home = { react: [], drafts: [], scheduled: [], runs: [], sent: [], loadedAt: null, error: null };
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Ф30-1: подписи', () => {
  it('sendAtLabel — «к ЧЧ:ММ, Д мес»; причины ошибок пачки и failed', () => {
    expect(sendAtLabel(AT)).toBe(`к ${formatTime(AT)}, 5 окт`);
    expect(sendAtLabel(new Date(2026, 4, 1, 9, 5).getTime())).toMatch(/, 1 мая$/);
    const results = [
      { id: 1, status: 'error' as const, error: 'send_at_passed', send_at_ts: AT, message: PASSED },
      { id: 2, status: 'error' as const, error: 'invalid transition' },
      { id: 3, status: 'queued' as const, paced: false, not_before_ts: AT, send_at_ts: AT },
    ];
    expect(batchErrors(results)).toEqual([`#1: ${PASSED}`, '#2: invalid transition']);
    expect(batchPassed(results)).toEqual([1]);
    expect(outboxErrorText('send_at_late')).toBe('опоздало: хаб не работал в назначенное время');
    expect(outboxErrorText('сеть')).toBe('сеть');
    expect(outboxErrorText(null)).toBeNull();
  });
});

describe('Ф30-1: карточка и строка черновика', () => {
  it('до подтверждения — «к 10:00, 5 окт»; черновик без send_at — без пометки', () => {
    const card = mount(DraftCard, { props: { item: draft(1) } });
    expect(card.get('.draft-card-send-at').text()).toBe(sendAtLabel(AT));
    card.unmount();
    const row = mount(DraftRow, { props: { item: draft(1) } });
    expect(row.get('.draft-card-send-at').text()).toBe(sendAtLabel(AT));
    row.unmount();
    const plain = mount(DraftCard, { props: { item: draft(2, { send_at_ts: null }) } });
    expect(plain.find('.draft-card-send-at').exists()).toBe(false);
    expect(plain.find('.draft-card-paced').exists()).toBe(false);
    plain.unmount();
  });

  it('карточка: 409 send_at_passed — «время прошло» и «Отправить сейчас» -> approve с send_now', async () => {
    const calls = stubFetch();
    store.state.home.drafts = [draft(5)];
    const wrapper = mount(DraftCard, { props: { item: draft(5) } });
    expect(wrapper.find('.draft-send-now').exists()).toBe(false);
    await wrapper.get('.draft-send').trigger('click');
    await flushPromises();
    expect(wrapper.get('.draft-send-at-passed').text()).toContain(PASSED);
    expect(approves(calls).map((c) => c.body)).toEqual([{ text: null }]);
    await wrapper.get('.draft-send-now').trigger('click');
    await flushPromises();
    expect(approves(calls).map((c) => c.body)).toEqual([{ text: null }, { text: null, send_now: true }]);
    expect(store.state.home.drafts).toEqual([]);
    wrapper.unmount();
  });

  it('строка: то же', async () => {
    const calls = stubFetch();
    const wrapper = mount(DraftRow, { props: { item: draft(6) } });
    await wrapper.get('.draft-send').trigger('click');
    await flushPromises();
    expect(wrapper.get('.draft-send-at-passed').text()).toContain(PASSED);
    await wrapper.get('.draft-send-now').trigger('click');
    await flushPromises();
    expect(approves(calls).at(-1)).toEqual({
      method: 'POST',
      path: '/api/v1/outbox/6/approve',
      body: { text: null, send_now: true },
    });
    wrapper.unmount();
  });

  it('другая ошибка 409 — обычный текст, без «Отправить сейчас»', async () => {
    stubFetch((path) => (path.endsWith('/approve') ? json({ detail: 'invalid transition' }, 409) : undefined));
    const wrapper = mount(DraftCard, { props: { item: draft(7) } });
    await wrapper.get('.draft-send').trigger('click');
    await flushPromises();
    expect(wrapper.find('.draft-send-now').exists()).toBe(false);
    expect(wrapper.get('.draft-card-error').text()).toBe('invalid transition');
    wrapper.unmount();
  });

  it('П3: подтверждённый на столбце — «Запланировано», «уйдёт в …» и «Отменить» -> cancel', async () => {
    const calls = stubFetch();
    const item = draft(8, { status: 'queued', not_before_ts: AT });
    store.state.home.scheduled = [item];
    const board = buildBoard(
      [project()],
      { react: [], drafts: [], scheduled: [item], runs: [], sent: [] },
      NOW,
    );
    const wrapper = mount(ProjectColumn, { props: { column: board.columns[0]! } });
    expect(wrapper.get('.board-scheduled-title').text()).toBe('Запланировано · 1');
    const row = wrapper.get('[data-outbox-id="8"]');
    expect(row.get('.draft-card-slot').text()).toMatch(/^уйдёт 05\.10 в /);
    await row.get('.draft-reject').trigger('click');
    await flushPromises();
    expect(calls.some((c) => c.path === '/api/v1/outbox/8/cancel' && c.method === 'POST')).toBe(true);
    wrapper.unmount();
  });

  it('П3: главная берёт из очереди и строки с send_at, не только по графику', async () => {
    const queued = [
      draft(11, { status: 'queued', not_before_ts: AT }),
      draft(12, { status: 'queued', not_before_ts: NOW, send_at_ts: null }),
      draft(13, { status: 'queued', paced: true, not_before_ts: AT - 1, send_at_ts: null }),
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        const url = new URL(input, 'http://localhost');
        if (url.pathname === '/api/v1/outbox' && url.searchParams.get('status') === 'queued') return json(queued);
        return json([]);
      }),
    );
    await store.loadHome();
    expect((store.state.home.scheduled ?? []).map((d) => d.id)).toEqual([13, 11]);
  });
});

describe('Ф30-1: лента беседы и пачка', () => {
  it('бейдж: «· к …» до подтверждения; после — «уйдёт …» без «(по графику)» и «Отменить»', async () => {
    const calls = stubFetch();
    const pending = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'pending_approval', text: 'Пост', sendAtTs: AT },
    });
    expect(pending.get('.outbox-label').text()).toBe(`черновик #9 · ждёт подтверждения · ${sendAtLabel(AT)}`);
    pending.unmount();

    const queued = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'queued', text: 'Пост', notBeforeTs: AT, sendAtTs: AT },
    });
    expect(queued.get('.outbox-label').text()).toMatch(/^#\d+ · уйдёт 05\.10 в \d\d:\d\d$/);
    const cancelButton = queued.findAll('.outbox-actions button').find((b) => b.text() === 'Отменить');
    await cancelButton!.trigger('click');
    await flushPromises();
    expect(calls.some((c) => c.path === '/api/v1/outbox/9/cancel')).toBe(true);
    queued.unmount();
  });

  it('бейдж: 409 -> «Отправить сейчас» с текстом правки', async () => {
    const calls = stubFetch();
    const wrapper = mount(OutboxStatusBadge, {
      props: { outboxId: 14, status: 'pending_approval', text: 'Пост', sendAtTs: AT },
    });
    await wrapper.get('textarea').setValue('Пост, правка');
    await wrapper.get('.outbox-actions button').trigger('click');
    await flushPromises();
    expect(wrapper.get('.outbox-send-at-passed').text()).toContain(PASSED);
    expect(wrapper.find('.outbox-action-error').exists()).toBe(false);
    await wrapper.get('.outbox-send-now').trigger('click');
    await flushPromises();
    expect(approves(calls).map((c) => c.body)).toEqual([
      { text: 'Пост, правка' },
      { text: 'Пост, правка', send_now: true },
    ]);
    wrapper.unmount();
  });

  it('бейдж: failed по опозданию — причина простыми словами', async () => {
    stubFetch();
    const wrapper = mount(OutboxStatusBadge, { props: { outboxId: 15, status: 'failed', text: 'Пост' } });
    await flushPromises();
    expect(wrapper.get('.outbox-label--error').text().replace(/^#\d+ · /, '')).toBe(
      'не отправлено — опоздало: хаб не работал в назначенное время',
    );
    wrapper.unmount();
  });

  it('пачка: ошибка только у просроченного, у него «Отправить сейчас»', async () => {
    const calls = stubFetch((path) =>
      path === '/api/v1/outbox/approve-batch'
        ? json({
            results: [
              { id: 21, status: 'error', error: 'send_at_passed', send_at_ts: AT, message: PASSED },
              { id: 22, status: 'queued', paced: false, not_before_ts: AT, send_at_ts: AT },
            ],
          })
        : undefined,
    );
    const items = [draft(21), draft(22)];
    store.state.home.drafts = items;
    store.selectDrafts([21, 22]);
    const wrapper = mount(DraftBatchBar, { props: { items } });
    await wrapper.get('.draft-approve-selected').trigger('click');
    await flushPromises();
    expect(wrapper.get('.draft-batch-result').text()).toBe(`подтверждено 1, запланировано на 05.10 в ${formatTime(AT)}`);
    expect(wrapper.get('.draft-batch-errors').text()).toBe(`#21: ${PASSED}`);
    const buttons = wrapper.findAll('.draft-send-now');
    expect(buttons.map((b) => b.text())).toEqual(['Отправить сейчас #21']);
    await buttons[0]!.trigger('click');
    await flushPromises();
    expect(approves(calls).at(-1)).toEqual({
      method: 'POST',
      path: '/api/v1/outbox/21/approve',
      body: { text: null, send_now: true },
    });
    expect(wrapper.find('.draft-send-now').exists()).toBe(false);
    expect(wrapper.find('.draft-batch-errors').exists()).toBe(false);
    wrapper.unmount();
  });
});
