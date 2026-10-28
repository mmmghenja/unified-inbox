// Ф52-2: «Слышит неразобранное аккаунтов» в панели «Раскладка» — `PATCH /projects/{id}` с `unsorted_accounts`.
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Project, StatusResponse } from '../src/api/types';
import MappingPanel from '../src/components/MappingPanel.vue';
import { store } from '../src/store/instance';

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, ...overrides };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function fakeFetch(projects: () => Project[], patchStatus = 200) {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: url.pathname,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    sent.push(s);
    if (s.path === '/api/v1/projects' && s.method === 'GET') return jsonResponse(projects());
    if (s.method === 'GET') return jsonResponse([]);
    if (s.method === 'PATCH' && patchStatus !== 200) {
      return jsonResponse({ detail: 'unsorted_accounts: нет аккаунтов 999' }, patchStatus);
    }
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

enableAutoUnmount(afterEach);

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.projects = [project({ unsorted_accounts: [8] })];
  store.state.status = {
    accounts: [
      { id: 5, source: 'mail', title: 'ящик' },
      { id: 8, source: 'telegram', title: null },
    ],
  } as unknown as StatusResponse;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('Ф52-2: неразобранное аккаунтов в «Раскладке»', () => {
  it('галочки отражают поле, клик шлёт полный список', async () => {
    const { sent, fetchMock } = fakeFetch(() => [project({ unsorted_accounts: [8] })]);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const boxes = wrapper.findAll('.mapping-project-unsorted-box');
    expect(boxes.map((b) => (b.element as HTMLInputElement).checked)).toEqual([false, true]);
    expect(wrapper.get('.mapping-project-unsorted').text()).toContain('mail · ящик');
    await boxes[0]!.setValue(true);
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/3');
    expect(patches.map((p) => p.body)).toEqual([{ unsorted_accounts: [5, 8] }]);
  });

  it('снятие последней галочки шлёт пустой список', async () => {
    const { sent, fetchMock } = fakeFetch(() => [project({ unsorted_accounts: [8] })]);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    await wrapper.findAll('.mapping-project-unsorted-box')[1]!.setValue(false);
    await flushPromises();
    expect(sent.filter((s) => s.method === 'PATCH').map((p) => p.body)).toEqual([{ unsorted_accounts: [] }]);
  });

  it('ошибка записи показывается', async () => {
    const { fetchMock } = fakeFetch(() => [project()], 422);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    await wrapper.findAll('.mapping-project-unsorted-box')[0]!.setValue(true);
    await flushPromises();
    expect(wrapper.text()).toContain('нет аккаунтов 999');
  });
});
