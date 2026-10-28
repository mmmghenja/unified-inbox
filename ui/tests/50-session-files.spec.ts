// Ф14-3 D (`docs/architecture.md`): файлы черновика в карточке — миниатюра у
// картинок, имя текстом, размер, «открыть»; «Claude приложил файлы: N»; «Отправить» при пустой подписи и файлах;
// флажка пачки у черновика с файлами нет, «Выбрать все» его не берёт.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OutboxListItem, Project } from '../src/api/types';
import DraftCard from '../src/components/DraftCard.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import { store } from '../src/store/instance';
import { buildBoard, type BoardColumn } from '../src/util/board';
import { startOfDay } from '../src/util/home';

const NOW = new Date(2026, 8, 28, 12, 0, 0).getTime();

function project(): Project {
  return { id: 3, name: 'Umbrella', color: null, sort: 0, muted: false, workdir: null, unread: 0 };
}

function draft(id: number, overrides: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id,
    channel_id: 100 + id,
    project_id: 3,
    channel_title: `Клиент ${id}`,
    text: `Черновик ${id}`,
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
    author: 'mcp:all',
    reply_to: null,
    thread: null,
    paced: false,
    not_before_ts: null,
    account_id: 1,
    files: [],
    ...overrides,
  };
}

const FILES = [
  { media_id: 501, name: 'скрин.png', size: 2048, kind: 'photo', mime: 'image/png' },
  { media_id: 502, name: '<b>dogovor</b>.pdf', size: 3 * 1024 * 1024, kind: 'document', mime: 'application/pdf' },
];

interface Recorded {
  method: string;
  path: string;
  body: unknown;
}

function stubFetch(): Recorded[] {
  const calls: Recorded[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null;
      calls.push({ method, path: url.pathname, body });
      const payload = url.pathname.endsWith('/approve')
        ? { status: 'queued', paced: false, not_before_ts: null }
        : url.pathname === '/api/v1/sync'
          ? { changes: [], max_seq: 0 }
          : [];
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } });
    }),
  );
  return calls;
}

function columnWith(drafts: OutboxListItem[]): BoardColumn {
  const board = buildBoard(
    [project()],
    { react: [], drafts, scheduled: [], runs: [], sent: [] },
    NOW,
  );
  return board.columns[0]!;
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.draftSelection = [];
  store.state.projects = [project()];
  store.state.home = { react: [], drafts: [], scheduled: [], runs: [], sent: [], loadedAt: null, error: null };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф14-3: файлы в карточке черновика', () => {
  it('миниатюра у картинки, имя текстом, размер, «открыть», пометка «Claude приложил файлы: 2»', () => {
    const wrapper = mount(DraftCard, { props: { item: draft(1, { files: FILES }) } });
    expect(wrapper.get('.draft-card-files-note').text()).toBe('Claude приложил файлы: 2');
    const rows = wrapper.findAll('.draft-card-file');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.get('img.draft-card-file-thumb').attributes('src')).toBe('/api/v1/media/501');
    expect(rows[1]!.find('img').exists()).toBe(false);
    expect(rows.map((r) => r.get('.draft-card-file-name').text())).toEqual(['скрин.png', '<b>dogovor</b>.pdf']);
    expect(rows[1]!.find('b').exists()).toBe(false); // имя — только текстом
    expect(rows[0]!.get('.draft-card-file-size').text()).toBe('2 КБ');
    expect(rows[1]!.get('.draft-card-file-size').text()).toBe('3 МБ');
    const open = rows[1]!.get('a.draft-card-file-open');
    expect(open.attributes('href')).toBe('/api/v1/media/502');
    expect(open.attributes('target')).toBe('_blank');
    expect(open.attributes('rel')).toContain('noopener');
    wrapper.unmount();
  });

  it('черновик владельца с файлами — без пометки Claude; без файлов — списка нет', () => {
    const mine = mount(DraftCard, { props: { item: draft(1, { origin: 'ui', files: FILES }) } });
    expect(mine.find('.draft-card-files-note').exists()).toBe(false);
    expect(mine.findAll('.draft-card-file')).toHaveLength(2);
    mine.unmount();
    const plain = mount(DraftCard, { props: { item: draft(2) } });
    expect(plain.find('.draft-card-files').exists()).toBe(false);
    plain.unmount();
  });

  it('«Отправить» при пустой подписи и файлах активна и зовёт approve; без файлов — нет', async () => {
    const calls = stubFetch();
    const withFiles = mount(DraftCard, { props: { item: draft(1, { text: '', files: FILES }) } });
    const send = withFiles.get('.draft-send');
    expect(send.attributes('disabled')).toBeUndefined();
    await send.trigger('click');
    await flushPromises();
    expect(calls.filter((c) => c.path === '/api/v1/outbox/1/approve')).toHaveLength(1);
    withFiles.unmount();

    const empty = mount(DraftCard, { props: { item: draft(2, { text: '' }) } });
    expect(empty.get('.draft-send').attributes('disabled')).toBeDefined();
    empty.unmount();
  });

  it('правка подписи до пустой у черновика с файлами — «Отправить» остаётся активной', async () => {
    const wrapper = mount(DraftCard, { props: { item: draft(1, { text: 'подпись', files: FILES }) } });
    await wrapper.get('.draft-edit').trigger('click');
    await wrapper.get('.draft-card-edit').setValue('   ');
    expect(wrapper.get('.draft-send').attributes('disabled')).toBeUndefined();
    wrapper.unmount();
  });

  it('флажка пачки у черновика с файлами нет, подсказка; «Выбрать все» берёт только без файлов', async () => {
    stubFetch();
    const drafts = [draft(1), draft(2, { files: FILES }), draft(3)];
    store.state.home.drafts = [...drafts];
    const wrapper = mount(ProjectColumn, { props: { column: columnWith(drafts) } });
    expect(wrapper.find('[data-outbox-id="2"] .draft-select').exists()).toBe(false);
    expect(wrapper.get('[data-outbox-id="2"] .draft-select-files').attributes('title')).toBe(
      'с файлами — только из карточки',
    );
    expect(wrapper.find('[data-outbox-id="1"] .draft-select').exists()).toBe(true);
    await wrapper.get('.draft-select-all').trigger('click');
    expect(store.state.draftSelection).toEqual([1, 3]);
    expect(wrapper.get('.draft-approve-selected').text()).toBe('Подтвердить выбранные (2)');
    wrapper.unmount();
  });
});
