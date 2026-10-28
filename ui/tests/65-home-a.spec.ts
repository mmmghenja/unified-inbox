// Ф20-4 (`docs/architecture.md`): «доска, но тонкая» — строка черновика `DraftRow` и окно
// `DraftModal`, столбец главной (без чата, карточки сессий в «Нужна реакция», «ещё N»,
// «Ничего не ждёт»), страница «Агенты» (`ClaudeView`), телефон: чипы без серого счётчика и «⋯» вместо кнопок.
// Меню «⋯» и тревоги полосы — в `38-shell.spec.ts`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OutboxListItem, Project } from '../src/api/types';
import ClaudeView from '../src/components/ClaudeView.vue';
import DraftRow from '../src/components/DraftRow.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import { store } from '../src/store/instance';
import { buildBoard } from '../src/util/board';
import { initLayout } from '../src/util/layout';
import { fakeMedia } from './media';

const NOW = new Date(2026, 9, 1, 12, 0, 0).getTime();

function project(overrides: Partial<Project>): Project {
  return { id: 1, name: 'Globex', color: '#ff8800', sort: 0, muted: false, workdir: '/w/m', unread: 0, ...overrides };
}

function outbox(id: number, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
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
    created_ts: NOW - id * 1000,
    sent_ts: null,
    ext_id: null,
    project_name: 'Globex',
    source: 'telegram',
    kind: 'dm',
    author: 'Claude · Globex',
    reply_to: null,
    thread: null,
    ...overrides,
  };
}

function columnFor(drafts: OutboxListItem[], scheduled: OutboxListItem[] = []) {
  const board = buildBoard(
    store.state.projects,
    { react: [], drafts, scheduled, runs: [], sent: [] },
    NOW,
    store.state.sessions,
  );
  return board.columns[0]!;
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.projects = [project({})];
  store.state.home = { react: [], drafts: [], runs: [], sent: [], loadedAt: null, error: null };
  store.state.homeOpen = true;
  store.state.claudeOpen = false;
  store.state.phoneTab = 'wait';
  store.state.draftSelection = [];
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  initLayout(null);
  document.body.innerHTML = '';
});

// --- DraftRow ------------------------------------------------------------------------------------------------------

describe('Ф20-4 C4: DraftRow — черновик одной строкой', () => {
  it('#id, кому/адрес, начало текста, «по графику»; «Отправить» и «Отклонить» зовут стор и блокируются на время вызова', async () => {
    let release: () => void = () => undefined;
    const approve = vi.spyOn(store, 'approveOutbox').mockImplementation(
      () => new Promise((resolve) => (release = () => resolve(undefined as never))),
    );
    const cancel = vi.spyOn(store, 'cancelOutbox').mockResolvedValue(undefined as never);
    const wrapper = mount(DraftRow, { props: { item: outbox(50, { paced: true }), selectable: true } });
    // Номер — тихой подписью рядом с адресатом (владелец 07.10: «номеров не вижу»); проект в строке не показывается.
    expect(wrapper.get('.draft-card-num').text()).toBe('#50');
    expect(wrapper.get('.draft-row-who').text()).toBe('Покупатель');
    expect(wrapper.get('.draft-row-who').attributes('title')).toBe('#50 · Покупатель');
    expect(wrapper.get('.draft-row-text').text()).toBe('текст черновика 50');
    expect(wrapper.get('.draft-card-paced').text()).toBe('по графику');
    expect(wrapper.find('input.draft-select').exists()).toBe(true);

    await wrapper.get('.draft-send').trigger('click');
    expect(approve).toHaveBeenCalledWith(50);
    expect(wrapper.get('.draft-send').attributes('disabled')).toBeDefined();
    expect(wrapper.get('.draft-reject').attributes('disabled')).toBeDefined();
    release();
    await flushPromises();
    expect(wrapper.get('.draft-send').attributes('disabled')).toBeUndefined();

    await wrapper.get('.draft-reject').trigger('click');
    expect(cancel).toHaveBeenCalledWith(50);
    wrapper.unmount();
  });

  it('кому: ответ на сообщение — `draftTargetLabel`; черновик с файлами — метка «с файлами» вместо флажка; ошибка — под строкой', async () => {
    vi.spyOn(store, 'approveOutbox').mockRejectedValue(new api.ApiError(409, 'уже ушёл', null));
    const item = outbox(51, { reply_to: { sender: 'Пётр', excerpt: 'Когда?', ext_id: 'm1' } as OutboxListItem['reply_to'] });
    const wrapper = mount(DraftRow, { props: { item, selectable: true } });
    expect(wrapper.get('.draft-row-who').text()).toBe('Покупатель');
    expect(wrapper.get('.draft-row-who').attributes('title')).toBe('#51 · ответ на: Пётр: Когда?');
    const phone = mount(DraftRow, { props: { item, showProject: true } });
    expect(phone.get('.draft-row-who').text()).toBe('Globex › Покупатель'); // «Ждёт» телефона — проекты вперемешку
    phone.unmount();
    await wrapper.get('.draft-send').trigger('click');
    await flushPromises();
    expect(wrapper.get('.draft-card-error').text()).toBe('уже ушёл');
    wrapper.unmount();

    const files = mount(DraftRow, {
      props: {
        item: outbox(52, { files: [{ media_id: 1, name: 'a.pdf', kind: 'file', size: 10 }] as OutboxListItem['files'] }),
        selectable: true,
      },
    });
    expect(files.find('input.draft-select').exists()).toBe(false);
    expect(files.get('.draft-select-files').text()).toBe('с файлами');
    files.unmount();
  });

  it('«›» открывает окно (диалог, aria-modal) с полной карточкой и «Открыть беседу»; Esc закрывает и возвращает фокус на «›»', async () => {
    const wrapper = mount(DraftRow, { props: { item: outbox(50) }, attachTo: document.body });
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    await wrapper.get('.draft-row-open').trigger('click');
    await flushPromises();
    const dialog = wrapper.get('[role="dialog"]');
    expect(dialog.attributes('aria-modal')).toBe('true');
    expect(dialog.find('.draft-card').exists()).toBe(true);
    expect(dialog.find('.draft-open').text()).toBe('Открыть беседу');
    expect(document.activeElement).toBe(dialog.element);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await flushPromises();
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    expect(document.activeElement).toBe(wrapper.get('.draft-row-open').element);
    wrapper.unmount();
  });

  it('окно закрывается по ✕ и клику мимо; клик внутри окна не закрывает', async () => {
    const wrapper = mount(DraftRow, { props: { item: outbox(50) }, attachTo: document.body });
    await wrapper.get('.draft-row-open').trigger('click');
    await wrapper.get('.draft-modal').trigger('click');
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true);
    await wrapper.get('.draft-modal-close').trigger('click');
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);

    await wrapper.get('.draft-row-open').trigger('click');
    await wrapper.get('.draft-modal-overlay').trigger('click');
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('после отправки, отклонения и «Открыть беседу» из окна оно закрывается само', async () => {
    const approve = vi.spyOn(store, 'approveOutbox').mockResolvedValue(undefined as never);
    const cancel = vi.spyOn(store, 'cancelOutbox').mockResolvedValue(undefined as never);
    const openAt = vi.spyOn(store, 'openChannelAt').mockResolvedValue(undefined as never);
    const wrapper = mount(DraftRow, { props: { item: outbox(50) }, attachTo: document.body });
    const open = async () => {
      await wrapper.get('.draft-row-open').trigger('click');
      await flushPromises();
    };

    await open();
    await wrapper.get('.draft-modal .draft-send').trigger('click');
    await flushPromises();
    expect(approve).toHaveBeenCalledWith(50, undefined);
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);

    await open();
    await wrapper.get('.draft-modal .draft-reject').trigger('click');
    await flushPromises();
    expect(cancel).toHaveBeenCalledWith(50);
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);

    await open();
    await wrapper.get('.draft-modal .draft-open').trigger('click');
    await flushPromises();
    expect(openAt).toHaveBeenCalled();
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('по графику (queued): когда уйдёт, «Отменить» и «›»; без «Отправить» и текста', async () => {
    const cancel = vi.spyOn(store, 'cancelOutbox').mockResolvedValue(undefined as never);
    const item = outbox(60, { status: 'queued', not_before_ts: Date.now() + 3600_000 });
    const wrapper = mount(DraftRow, { props: { item } });
    expect(wrapper.find('.draft-send').exists()).toBe(false);
    expect(wrapper.find('.draft-row-text').exists()).toBe(false);
    expect(wrapper.find('.draft-card-slot').exists()).toBe(true);
    expect(wrapper.find('.draft-row-open').exists()).toBe(true);
    await wrapper.get('.draft-reject').trigger('click');
    expect(cancel).toHaveBeenCalledWith(60);
    wrapper.unmount();
  });
});

// --- столбец -------------------------------------------------------------------------------------------------------

describe('Ф20-4 C: столбец главной', () => {
  it('пусто — «Ничего не ждёт»; серая строка про Claude', () => {
    const wrapper = mount(ProjectColumn, { props: { column: columnFor([]) } });
    expect(wrapper.get('.board-nothing').text()).toBe('Ничего не ждёт');
    expect(wrapper.get('.board-claude-line').text()).toContain('сессия: свободна');
    wrapper.unmount();
  });

  it('карточка сессии — в «Нужна реакция»; «Claude сегодня» и сессия «ждёт»', () => {
    store.state.sessions.items = { 5: { id: 5, project_id: 1, closed_ts: null, pending_requests: 1 } as never };
    store.state.sessions.pending = {
      5: [{ id: 11, session_id: 5, project_id: 1, kind: 'permission', tool: 'Bash', input: { command: 'ls' }, status: 'pending' } as never],
    };
    const wrapper = mount(ProjectColumn, { props: { column: columnFor([]) } });
    const react = wrapper.get('[data-section="react"]');
    expect(react.get('.board-section-title').text()).toBe('Нужна реакция · 1');
    expect(react.find('.work-request[data-request-id="11"]').exists()).toBe(true);
    expect(wrapper.find('.board-nothing').exists()).toBe(false);
    expect(wrapper.get('.board-claude-line').text()).toContain('сессия: ждёт');
    wrapper.unmount();
    store.state.sessions.items = {};
    store.state.sessions.pending = {};
  });

  it('12 черновиков — видно 8 строк, «ещё 4 ▾» раскрывает все; над строками пачка', async () => {
    const drafts = Array.from({ length: 12 }, (_, i) => outbox(i + 1));
    const wrapper = mount(ProjectColumn, { props: { column: columnFor(drafts) } });
    expect(wrapper.get('[data-section="drafts"] .board-section-title').text()).toBe('Черновики · 12');
    expect(wrapper.find('.draft-batch-bar').exists()).toBe(true);
    expect(wrapper.findAll('.draft-row')).toHaveLength(8);
    const more = wrapper.get('.draft-more');
    expect(more.text()).toBe('ещё 4 ▾');
    await more.trigger('click');
    expect(wrapper.findAll('.draft-row')).toHaveLength(12);
    wrapper.unmount();
  });

  it('«По графику» — строками; «Агенты ›» открывает страницу «Агенты»', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    const scheduled = [outbox(70, { status: 'queued', not_before_ts: Date.now() + 3600_000 })];
    const wrapper = mount(ProjectColumn, { props: { column: columnFor([], scheduled) } });
    expect(wrapper.get('[data-section="scheduled"] .board-section-title').text()).toBe('По графику · 1');
    expect(wrapper.findAll('[data-section="scheduled"] .draft-row')).toHaveLength(1);
    await wrapper.get('.board-claude-link').trigger('click');
    expect(store.state.claudeOpen).toBe(true);
    expect(store.state.boardFocus).toMatchObject({ projectId: 1 });
    store.state.claudeOpen = false;
    store.state.homeOpen = true;
    store.state.boardFocus = null;
    wrapper.unmount();
  });
});

// --- страница «Агенты» -----------------------------------------------------------------------------------------------

describe('Ф78-1: страница «Агенты»', () => {
  it('только список агентов и переписка агентов — секций проектов с задачами и «Новой задачи» нет', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    store.state.projects = [project({ id: 1 }), project({ id: 4, name: 'Личное', workdir: null })];
    const wrapper = mount(ClaudeView, { props: { phone: true } });
    await flushPromises();
    expect(wrapper.attributes('aria-label')).toBe('Агенты');
    expect(wrapper.find('.agents-block').exists()).toBe(true);
    expect(wrapper.find('.agent-traffic').exists()).toBe(true);
    expect(wrapper.find('.claude-project').exists()).toBe(false);
    expect(wrapper.find('.board-new-task').exists()).toBe(false);
    wrapper.unmount();
  });
});

// --- телефон -------------------------------------------------------------------------------------------------------

describe('Ф20-4 A4: телефон', () => {
  it('в верхней полосе «⋯» вместо кнопок движка, «Раскладки» и «Доступа»; чипы «Бесед» без серого счётчика', async () => {
    initLayout(fakeMedia(390).matchMedia);
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    store.state.projects = [
      project({ id: 1, name: 'Initech', unread: 9, unread_react: 2 }),
      project({ id: 3, name: 'Umbrella', unread: 4, unread_react: 0 }),
      project({ id: -2, name: 'Все', color: null, unread: 18, unread_react: 3 }),
    ];
    store.state.phoneTab = 'chats';
    store.state.homeOpen = false;
    store.state.currentProjectId = 1;
    const wrapper = mount(PhoneShell);
    await flushPromises();
    const top = wrapper.get('header.phone-top');
    expect(top.find('.top-menu-button').exists()).toBe(true);
    expect(top.find('.engine-chip').exists()).toBe(false);
    expect(top.find('.mapping-open').exists()).toBe(false);
    expect(top.find('.access-open').exists()).toBe(false);
    // красный остаётся, серого нет
    expect(wrapper.findAll('.project-chips .badge-react').map((b) => b.text())).toEqual(['2']);
    expect(wrapper.find('.project-chips .badge-quiet').exists()).toBe(false);
    expect(wrapper.find('.project-chip[data-project-id="3"] .badge').exists()).toBe(false);
    await top.get('.top-menu-button').trigger('click');
    expect(top.find('.engine-chip').exists()).toBe(true);
    wrapper.unmount();
    store.state.homeOpen = true;
    store.state.currentProjectId = null;
  });

  it('Ф41-6б: четвёртая вкладка — «Сессии»; «Агенты» — страница ClaudeView в одну колонку без вкладки', async () => {
    initLayout(fakeMedia(390).matchMedia);
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse([])));
    store.state.phoneTab = 'orch';
    const wrapper = mount(PhoneShell);
    await flushPromises();
    expect(wrapper.get('.phone-tab--orch').text()).toBe('Сессии');
    expect(wrapper.get('.phone-title').text()).toBe('Сессии');
    expect(wrapper.find('main.phone-sessions').exists()).toBe(true);
    store.state.phoneTab = 'dispatch';
    await flushPromises();
    expect(wrapper.get('.phone-title').text()).toBe('Агенты');
    expect(wrapper.find('main.claude-view.claude-view--phone').exists()).toBe(true);
    wrapper.unmount();
  });
});
