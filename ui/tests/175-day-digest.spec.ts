// Ф86-1 (`docs/architecture.md`): две галочки у проекта в «⋯ → Раскладка» — «Сводка дня
// сессиям после сжатия» (`digest_after_compact`) и «мои слова из соседних сессий» (`sibling_says`). Нет поля
// (старое ядро) — галочка стоит: по умолчанию оба включены.
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Project } from '../src/api/types';
import MappingPanel from '../src/components/MappingPanel.vue';
import { store } from '../src/store/instance';

enableAutoUnmount(afterEach);

function project(over: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, ...over };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function fakeFetch(projects: () => Project[]) {
  const sent: { method: string; path: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const method = init?.method ?? 'GET';
    sent.push({ method, path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url.pathname === '/api/v1/projects' && method === 'GET') return jsonResponse(projects());
    if (method === 'GET') return jsonResponse([]);
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф86-1: галочки сводки дня и соседних сессий', () => {
  it('нет полей — обе стоят', async () => {
    store.state.projects = [project()];
    const { fetchMock } = fakeFetch(() => store.state.projects);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    expect((wrapper.get('.mapping-project-digest-box').element as HTMLInputElement).checked).toBe(true);
    expect((wrapper.get('.mapping-project-siblings-box').element as HTMLInputElement).checked).toBe(true);
    expect(wrapper.get('.mapping-project-digest').text()).toContain('Сводка дня');
  });

  it('снять и поставить — PATCH своим ключом', async () => {
    let current = [project({ digest_after_compact: true, sibling_says: false })];
    store.state.projects = current;
    const { sent, fetchMock } = fakeFetch(() => current);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    expect((wrapper.get('.mapping-project-siblings-box').element as HTMLInputElement).checked).toBe(false);

    current = [project({ digest_after_compact: false, sibling_says: false })];
    await wrapper.get('.mapping-project-digest-box').setValue(false);
    await flushPromises();
    current = [project({ digest_after_compact: false, sibling_says: true })];
    await wrapper.get('.mapping-project-siblings-box').setValue(true);
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/3').map((s) => s.body);
    expect(patches).toEqual([{ digest_after_compact: false }, { sibling_says: true }]);
    expect((wrapper.get('.mapping-project-digest-box').element as HTMLInputElement).checked).toBe(false);
    expect((wrapper.get('.mapping-project-siblings-box').element as HTMLInputElement).checked).toBe(true);
  });
});
