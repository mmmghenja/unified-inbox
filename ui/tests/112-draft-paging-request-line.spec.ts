// Владелец 12.10.2026: черновики проекта листаются в окне черновика, после отправки окно переходит к следующему;
// разрешение в блоке «Ждёт тебя» — одной строкой («развернуть» / «открыть окном»).
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { OutboxListItem, SessionRequest } from '../src/api/types';
import DraftModalHost from '../src/components/DraftModalHost.vue';
import DraftRow from '../src/components/DraftRow.vue';
import WorkRequestCard from '../src/components/WorkRequestCard.vue';
import { draftModal, draftSiblings, nextAfter } from '../src/store/draftModal';
import { store } from '../src/store/instance';

// Ф78-1: карточка получает решение, повтор и «убрать» от хозяина — в этих тестах они не вызываются.
const HANDLERS = { decide: async () => 'ok' as const, retry: async () => 'ok' as const, dismiss: () => undefined };

function outbox(id: number, over: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: 7,
    project_id: 1,
    channel_title: 'Покупатель',
    text: `текст черновика ${id}`,
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: id * 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Globex',
    source: 'telegram',
    kind: 'dm',
    author: 'Claude · Globex',
    reply_to: null,
    thread: null,
    ...over,
  } as OutboxListItem;
}

const dialogs = () => [...document.body.querySelectorAll('[role="dialog"]')];
const shownId = () => dialogs().map((d) => d.getAttribute('aria-label'));

beforeEach(() => {
  draftModal.id = null;
  store.state.home.drafts = [outbox(3), outbox(1), outbox(2), outbox(9, { project_id: 2 })];
});

afterEach(() => {
  vi.restoreAllMocks();
  draftModal.id = null;
  store.state.home.drafts = [];
  document.body.innerHTML = '';
});

describe('порядок и переход', () => {
  it('draftSiblings: только свой проект, старые первыми; чужого списку черновика — он один', () => {
    expect(draftSiblings(store.state.home.drafts, outbox(2)).map((d) => d.id)).toEqual([1, 2, 3]);
    expect(draftSiblings(store.state.home.drafts, outbox(50)).map((d) => d.id)).toEqual([50]);
  });

  it('nextAfter: следующий по прежнему порядку, у последнего — предыдущий, никого — null', () => {
    expect(nextAfter([1, 2, 3], 2, new Set([1, 3]))).toBe(3);
    expect(nextAfter([1, 2, 3], 3, new Set([1, 2]))).toBe(2);
    expect(nextAfter([1, 2, 3], 2, new Set())).toBeNull();
    expect(nextAfter([], 2, new Set([1]))).toBeNull();
  });
});

describe('окно черновика: листание по проекту', () => {
  it('«›» листает к следующему, «‹» назад; счётчик «N из M»; чужой проект не входит', async () => {
    const host = mount(DraftModalHost, { attachTo: document.body });
    const row = mount(DraftRow, { props: { item: store.state.home.drafts[1]! }, attachTo: document.body });
    await row.get('.draft-row-open').trigger('click');
    await flushPromises();
    expect(shownId()).toEqual(['Черновик #1']);
    expect(document.body.querySelector('.draft-modal-count')!.textContent).toContain('1 из 3');
    expect((document.body.querySelector('.draft-modal-prev') as HTMLButtonElement).disabled).toBe(true);

    (document.body.querySelector('.draft-modal-next') as HTMLButtonElement).click();
    await flushPromises();
    expect(shownId()).toEqual(['Черновик #2']); // окно строки закрылось, показывает общий хозяин
    expect(document.body.querySelector('.draft-modal-count')!.textContent).toContain('2 из 3');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    await flushPromises();
    expect(shownId()).toEqual(['Черновик #3']);
    expect((document.body.querySelector('.draft-modal-next') as HTMLButtonElement).disabled).toBe(true);
    (document.body.querySelector('.draft-modal-prev') as HTMLButtonElement).click();
    await flushPromises();
    expect(shownId()).toEqual(['Черновик #2']);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(dialogs()).toEqual([]);
    expect(draftModal.id).toBeNull();
    row.unmount();
    host.unmount();
  });

  it('после отправки — следующий черновик проекта; после последнего окно закрывается', async () => {
    vi.spyOn(store, 'approveOutbox').mockImplementation(async (id: number) => {
      store.state.home.drafts = store.state.home.drafts.filter((d) => d.id !== id);
      return undefined as never;
    });
    const host = mount(DraftModalHost, { attachTo: document.body });
    const row = mount(DraftRow, { props: { item: store.state.home.drafts[2]! }, attachTo: document.body });
    await row.get('.draft-row-open').trigger('click');
    await flushPromises();
    expect(shownId()).toEqual(['Черновик #2']);
    const send = async () => {
      (document.body.querySelector('.draft-modal .draft-send') as HTMLButtonElement).click();
      await flushPromises();
    };
    await send();
    expect(shownId()).toEqual(['Черновик #3']);
    await send();
    expect(shownId()).toEqual(['Черновик #1']); // последним в порядке был 3 — назад, к оставшемуся
    await send();
    expect(dialogs()).toEqual([]);
    expect(store.state.home.drafts.map((d) => d.id)).toEqual([9]);
    row.unmount();
    host.unmount();
  });

  it('«Открыть беседу» закрывает окно, а не листает; стрелки в поле правки не листают', async () => {
    vi.spyOn(store, 'openChannelAt').mockResolvedValue(undefined as never);
    const host = mount(DraftModalHost, { attachTo: document.body });
    draftModal.id = 2;
    await flushPromises();
    const field = document.createElement('textarea');
    document.body.querySelector('.draft-modal')!.appendChild(field);
    field.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    await flushPromises();
    expect(shownId()).toEqual(['Черновик #2']);
    (document.body.querySelector('.draft-modal .draft-open') as HTMLButtonElement).click();
    await flushPromises();
    expect(dialogs()).toEqual([]);
    host.unmount();
  });
});

describe('разрешение одной строкой («Ждёт тебя»)', () => {
  const request = {
    id: 1,
    session_id: 5,
    project_id: 3,
    kind: 'permission',
    tool: 'Bash',
    input: { command: 'git push origin dev\necho вторая', description: 'пуш в dev' },
    input_truncated: false,
    reason: 'push под кнопкой',
    status: 'pending',
    answer: null,
    created_ts: 1,
    decided_ts: null,
    delivered: false,
    seq: 1,
    delegate: null,
    decided_by: null,
  } as unknown as SessionRequest;

  it('oneLine: строка с началом команды и кнопками, без поля комментария; «развернуть» — полная карточка', async () => {
    const w = mount(WorkRequestCard, { props: { ...HANDLERS, request, oneLine: true }, attachTo: document.body });
    expect(w.classes()).toContain('work-request--line');
    expect(w.get('[data-test="request-preview"]').classes()).toContain('work-request-preview--one');
    expect(w.find('.work-deny-comment').exists()).toBe(false);
    expect(w.find('button.work-allow').exists() && w.find('button.work-deny').exists()).toBe(true);
    expect(w.find('.work-request-reason').exists()).toBe(false);

    await w.get('button.work-request-expand').trigger('click');
    expect(w.classes()).not.toContain('work-request--line');
    expect(w.find('.work-deny-comment').exists()).toBe(true);
    expect(w.get('.work-request-reason').text()).toContain('push под кнопкой');

    await w.get('button.work-request-open').trigger('click');
    expect(document.body.querySelector('.request-text-modal .request-text-command')!.textContent).toContain('echo вторая');
    w.unmount();
  });
});
