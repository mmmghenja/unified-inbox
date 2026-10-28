// Ф12-2 C (`docs/architecture.md`): флажки у черновиков, «Выбрать все» и «Подтвердить
// выбранные (N)» — `approve-batch` в порядке выбора, итог строкой и ошибки по пунктам; пометки «по графику» /
// «уйдёт в …» / «завтра в …» и «Отменить» у запланированного — на главной и в ленте беседы.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { ApproveBatchResult, OutboxListItem, Project } from '../src/api/types';
import DraftCard from '../src/components/DraftCard.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { buildBoard, type BoardColumn } from '../src/util/board';
import { formatTime, startOfDay } from '../src/util/home';
import { batchErrors, batchSummary, slotLabel } from '../src/util/pace';

const NOW = new Date(2026, 8, 28, 12, 0, 0).getTime();
const AT_1430 = new Date(2026, 8, 28, 14, 30, 0).getTime();
const TOMORROW_1007 = new Date(2026, 8, 29, 10, 7, 0).getTime();

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: null, sort: 0, muted: false, workdir: null, unread: 0, ...overrides };
}

function draft(id: number, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: 100 + id,
    project_id: 3,
    channel_title: `@lead_${id}`,
    text: `Касание ${id}`,
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: NOW - (10 - id) * 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Umbrella',
    source: 'telegram',
    kind: 'dm',
    author: 'mcp:iip',
    reply_to: null,
    thread: null,
    paced: true,
    not_before_ts: null,
    account_id: 1,
    ...overrides,
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

interface Recorded {
  method: string;
  path: string;
  body: unknown;
}

/** `pending` — что ядро отдаёт на `GET /outbox?status=pending_approval` (стор перечитывает после пачки). */
function stubFetch(
  results: (ids: number[]) => ApproveBatchResult[],
  pending: () => OutboxListItem[] = () => [],
): Recorded[] {
  const calls: Recorded[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null;
      calls.push({ method, path: url.pathname, body });
      if (url.pathname === '/api/v1/outbox/approve-batch') {
        return jsonResponse({ results: results((body as { ids: number[] }).ids) });
      }
      if (url.pathname.endsWith('/cancel')) return jsonResponse({ status: 'cancelled' });
      if (url.pathname === '/api/v1/sync') return jsonResponse({ changes: [], max_seq: 0 });
      if (url.pathname === '/api/v1/outbox' && url.searchParams.get('status') === 'pending_approval') {
        return jsonResponse(pending());
      }
      return jsonResponse([]);
    }),
  );
  return calls;
}

function columnWith(drafts: OutboxListItem[], scheduled: OutboxListItem[] = []): BoardColumn {
  const board = buildBoard(
    [project()],
    { react: [], drafts, scheduled, runs: [], sent: [] },
    NOW,
  );
  return board.columns[0]!;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.draftSelection = [];
  store.state.homeOpen = false;
  store.state.projects = [project()];
  store.state.home = { react: [], drafts: [], scheduled: [], runs: [], sent: [], loadedAt: null, error: null };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('Ф12-2: подписи графика', () => {
  it('день проекта закрыт — черновики его столбца на доске не показываются (Ф64-1)', () => {
    const board = buildBoard(
      [project({ workday: true, day_open: false })],
      { react: [], drafts: [draft(1)], scheduled: [], runs: [], sent: [] },
      NOW,
    );
    expect(board.columns[0]!.drafts).toEqual([]);
    expect(columnWith([draft(1)]).drafts).toHaveLength(1);
  });

  it('slotLabel: сегодня — «уйдёт в ЧЧ:ММ», завтра — «завтра в ЧЧ:ММ», дальше — с датой', () => {
    expect(slotLabel(AT_1430, NOW)).toBe(`уйдёт в ${formatTime(AT_1430)}`);
    expect(slotLabel(TOMORROW_1007, NOW)).toBe(`завтра в ${formatTime(TOMORROW_1007)}`);
    const later = new Date(2026, 8, 30, 10, 0, 0).getTime();
    expect(slotLabel(later, NOW)).toMatch(/^уйдёт 30\.09 в /);
  });

  it('batchSummary / batchErrors: подтверждено N, запланировано на первое–последнее; ошибки по пунктам', () => {
    const results: ApproveBatchResult[] = [
      { id: 1, status: 'queued', paced: true, not_before_ts: AT_1430 },
      { id: 2, status: 'error', error: 'invalid transition' },
      { id: 3, status: 'queued', paced: true, not_before_ts: TOMORROW_1007 },
    ];
    expect(batchSummary(results, NOW)).toBe(
      `подтверждено 2, запланировано на ${formatTime(AT_1430)}–завтра ${formatTime(TOMORROW_1007)}`,
    );
    expect(batchErrors(results)).toEqual(['#2: invalid transition']);
    expect(batchSummary([{ id: 5, status: 'queued', paced: false }], NOW)).toBe('подтверждено 1');
    expect(batchSummary([{ id: 5, status: 'queued', paced: true, not_before_ts: AT_1430 }], NOW)).toBe(
      `подтверждено 1, запланировано на ${formatTime(AT_1430)}`,
    );
  });
});

describe('Ф12-2: подтверждение пачкой', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
  });

  it('флажки -> «Подтвердить выбранные (N)» зовёт approve-batch в порядке выбора; итог и ошибки по пунктам', async () => {
    const drafts = [draft(1), draft(2), draft(3)];
    const calls = stubFetch(
      (ids) => [
        { id: ids[0]!, status: 'queued', paced: true, not_before_ts: AT_1430 },
        { id: ids[1]!, status: 'error', error: 'invalid transition' },
      ],
      () => drafts.filter((d) => d.id !== 3),
    );
    store.state.home.drafts = [...drafts];
    const wrapper = mount(ProjectColumn, { props: { column: columnWith(drafts) } });
    const approve = wrapper.get('.draft-approve-selected');
    expect(approve.text()).toBe('Подтвердить выбранные (0)');
    expect(approve.attributes('disabled')).toBeDefined();

    // выбор: третий, потом первый
    await wrapper.get('[data-outbox-id="3"] .draft-select').trigger('change');
    await wrapper.get('[data-outbox-id="1"] .draft-select').trigger('change');
    expect(approve.text()).toBe('Подтвердить выбранные (2)');
    await approve.trigger('click');
    await flushPromises();

    const batch = calls.filter((c) => c.path === '/api/v1/outbox/approve-batch');
    expect(batch).toHaveLength(1);
    expect(batch[0]!.method).toBe('POST');
    expect(batch[0]!.body).toEqual({ ids: [3, 1] });
    expect(wrapper.get('.draft-batch-result').text()).toBe(`подтверждено 1, запланировано на ${formatTime(AT_1430)}`);
    expect(wrapper.findAll('.draft-batch-errors li').map((li) => li.text())).toEqual(['#1: invalid transition']);
    // подтверждённый ушёл из черновиков и из выбора, ошибочный остался выбранным
    expect(store.state.home.drafts.map((d) => d.id)).toEqual([1, 2]);
    expect(store.state.draftSelection).toEqual([1]);
    wrapper.unmount();
  });

  it('«Выбрать все» — все черновики списка в порядке списка, уже выбранные остаются первыми', async () => {
    const calls = stubFetch((ids) => ids.map((id) => ({ id, status: 'queued' as const, paced: true, not_before_ts: AT_1430 })));
    const drafts = [draft(1), draft(2), draft(3)];
    store.state.home.drafts = [...drafts];
    const wrapper = mount(ProjectColumn, { props: { column: columnWith(drafts) } });
    await wrapper.get('[data-outbox-id="2"] .draft-select').trigger('change');
    await wrapper.get('.draft-select-all').trigger('click');
    expect(store.state.draftSelection).toEqual([2, 1, 3]);
    expect(wrapper.get('.draft-select-all').attributes('disabled')).toBeDefined();
    expect(wrapper.findAll('.draft-select').every((c) => (c.element as HTMLInputElement).checked)).toBe(true);
    await wrapper.get('.draft-approve-selected').trigger('click');
    await flushPromises();
    expect(calls.find((c) => c.path === '/api/v1/outbox/approve-batch')!.body).toEqual({ ids: [2, 1, 3] });
    expect(wrapper.get('.draft-batch-result').text()).toBe(`подтверждено 3, запланировано на ${formatTime(AT_1430)}`);
    wrapper.unmount();
  });

  it('повторный клик по флажку снимает выбор', async () => {
    stubFetch(() => []);
    const drafts = [draft(1), draft(2)];
    const wrapper = mount(ProjectColumn, { props: { column: columnWith(drafts) } });
    const box = wrapper.get('[data-outbox-id="1"] .draft-select');
    await box.trigger('change');
    await box.trigger('change');
    expect(store.state.draftSelection).toEqual([]);
    expect(wrapper.get('.draft-approve-selected').text()).toBe('Подтвердить выбранные (0)');
    wrapper.unmount();
  });

  it('телефон, «Ждёт»: те же флажки и кнопки над черновиками', async () => {
    const drafts = [draft(1), draft(2)];
    stubFetch(() => [], () => drafts);
    store.state.home.drafts = [...drafts];
    const wrapper = mount(WaitList);
    await flushPromises();
    expect(wrapper.findAll('.draft-select')).toHaveLength(2);
    await wrapper.get('.draft-select-all').trigger('click');
    expect(wrapper.get('.draft-approve-selected').text()).toBe('Подтвердить выбранные (2)');
    wrapper.unmount();
  });
});

describe('Ф12-2: пометки графика', () => {
  it('черновик по графику до подтверждения — «по графику»; обычный — без пометки', () => {
    const paced = mount(DraftCard, { props: { item: draft(1) } });
    expect(paced.get('.draft-card-paced').text()).toBe('по графику');
    expect(paced.find('.draft-select').exists()).toBe(false); // флажок — только в списках (`selectable`)
    paced.unmount();
    const plain = mount(DraftCard, { props: { item: draft(2, { paced: false }) } });
    expect(plain.find('.draft-card-paced').exists()).toBe(false);
    plain.unmount();
  });

  it('запланированный на столбце — «уйдёт в …», «Отправить сейчас» и «Отменить»; отмена зовёт cancel', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const calls = stubFetch(() => []);
    const item = draft(4, { status: 'queued', not_before_ts: AT_1430 });
    store.state.home.scheduled = [item];
    const wrapper = mount(ProjectColumn, { props: { column: columnWith([], [item]) } });
    expect(wrapper.get('.board-scheduled-title').text()).toBe('По графику · 1');
    const card = wrapper.get('[data-outbox-id="4"]');
    expect(card.get('.draft-card-slot').text()).toBe(`уйдёт в ${formatTime(AT_1430)}`);
    expect(card.find('.draft-send').exists()).toBe(false);
    expect(card.find('.draft-edit').exists()).toBe(false);
    expect(card.find('.draft-select').exists()).toBe(false);
    await card.get('.draft-reject').trigger('click');
    await flushPromises();
    expect(calls.some((c) => c.path === '/api/v1/outbox/4/cancel' && c.method === 'POST')).toBe(true);
    expect(store.state.home.scheduled).toEqual([]);
    wrapper.unmount();
  });

  it('в ленте беседы: «ждёт подтверждения · по графику», после — «завтра в ЧЧ:ММ (по графику)» и «Отменить»', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
    const calls = stubFetch(() => []);
    const pending = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'pending_approval', text: 'Касание', paced: true, notBeforeTs: null },
    });
    expect(pending.get('.outbox-label').text()).toBe('черновик #9 · ждёт подтверждения · по графику');
    pending.unmount();

    const queued = mount(OutboxStatusBadge, {
      props: { outboxId: 9, status: 'queued', text: 'Касание', paced: true, notBeforeTs: TOMORROW_1007 },
    });
    expect(queued.get('.outbox-label').text()).toMatch(/^#\d+ · /);
    expect(queued.get('.outbox-label').text().replace(/^#\d+ · /, '')).toBe(`завтра в ${formatTime(TOMORROW_1007)} (по графику)`);
    const cancelButton = queued.findAll('.outbox-actions button').find((b) => b.text() === 'Отменить');
    await cancelButton!.trigger('click');
    await flushPromises();
    expect(calls.some((c) => c.path === '/api/v1/outbox/9/cancel')).toBe(true);
    queued.unmount();

    const plain = mount(OutboxStatusBadge, { props: { outboxId: 10, status: 'queued', text: 'Ответ' } });
    expect(plain.get('.outbox-label').text().replace(/^#\d+ · /, '')).toBe('в очереди');
    expect(plain.find('.outbox-actions').exists()).toBe(false);
    plain.unmount();
  });
});
