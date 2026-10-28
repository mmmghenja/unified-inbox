// Ф48-4 (`docs/architecture.md`): сообщения агентов пачкой в интерфейсе —
// «в пачке до ЧЧ:ММ» у недоставленного сообщения в «Переписке агентов» и поле проекта «Сообщения агентов
// пачкой, мин» в панели «Раскладка» (`PATCH /projects/{id}` с `agent_batch_min`).
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { PeerMsg, PeerSide, Project, StatusResponse } from '../src/api/types';
import AgentTraffic from '../src/components/AgentTraffic.vue';
import MappingPanel from '../src/components/MappingPanel.vue';
import { store } from '../src/store/instance';
import { applyPeerMsg, batchLabel, batchUntil, createPeerMsgState } from '../src/store/peerMsgs';

const NOW = new Date(2026, 9, 9, 14, 0, 0).getTime();
const MIN = 60_000;

function side(overrides: Partial<PeerSide> = {}): PeerSide {
  return { project_id: 3, role: 'маркетолог', role_no: 1, sub_id: 31, ...overrides };
}

function msg(overrides: Partial<PeerMsg> = {}): PeerMsg {
  return {
    id: 1,
    from_user: false,
    to_user: false,
    from: side(),
    to: side({ role: 'аналитик', sub_id: 32 }),
    text: 'привет',
    reply_to: null,
    chain_id: null,
    hop: 0,
    status: 'pending',
    reason: null,
    created_ts: NOW - MIN,
    signaled_ts: null,
    signals: 0,
    read_ts: null,
    closed_ts: null,
    expires_ts: null,
    seq: 1,
    ...overrides,
  };
}

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

function fakeFetch(projects: () => Project[]): { sent: Sent[]; fetchMock: ReturnType<typeof vi.fn> } {
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
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

enableAutoUnmount(afterEach);

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.peerMsgs = createPeerMsgState();
  store.state.projects = [project()];
  store.state.status = { accounts: [] } as unknown as StatusResponse;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

describe('Ф48-4: «в пачке до» в переписке агентов', () => {
  it('срок впереди, сообщение ещё не сигналило — срок; иначе нет', () => {
    const until = NOW + 10 * MIN;
    expect(batchUntil(msg({ batch_ts: until }), NOW)).toBe(until);
    expect(batchLabel(msg({ batch_ts: until }), NOW)).toBe('в пачке до 14:10');
    expect(batchUntil(msg({ batch_ts: NOW }), NOW)).toBeNull(); // срок пришёл
    expect(batchUntil(msg({ batch_ts: until, signals: 1 }), NOW)).toBeNull(); // уже доставлено
    expect(batchUntil(msg({ batch_ts: until, status: 'read' }), NOW)).toBeNull();
    expect(batchUntil(msg({ batch_ts: null }), NOW)).toBeNull();
    expect(batchLabel(msg(), NOW)).toBe(''); // старое ядро поля не отдаёт
  });

  it('строка переписки показывает пометку только у ждущего пачки', () => {
    applyPeerMsg(store.state.peerMsgs, msg({ id: 1, text: 'сразу' }));
    applyPeerMsg(store.state.peerMsgs, msg({ id: 2, text: 'в пачке', batch_ts: NOW + 5 * MIN }));
    const wrapper = mount(AgentTraffic, { props: { nowMs: NOW } });
    const rows = wrapper.findAll('.traffic-row');
    expect(rows.map((r) => r.get('.traffic-text').text())).toEqual(['в пачке', 'сразу']);
    expect(rows[0]!.get('.traffic-batch').text()).toBe('в пачке до 14:05');
    expect(rows[1]!.find('.traffic-batch').exists()).toBe(false);
  });
});

describe('Ф48-4: «Раскладка» — сообщения агентов пачкой', () => {
  it('поле показывает умолчание проекта и шлёт PATCH agent_batch_min', async () => {
    let current = [project({ agent_batch_min: 5 })];
    store.state.projects = current;
    const { sent, fetchMock } = fakeFetch(() => current);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const input = wrapper.get('.mapping-project-agent-batch-min');
    expect((input.element as HTMLInputElement).value).toBe('5');
    expect(wrapper.get('.mapping-project-agent-batch').text()).toContain('Сообщения агентов пачкой, мин');

    current = [project({ agent_batch_min: 15 })];
    await input.setValue('15');
    await flushPromises();
    const patches = sent.filter((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/3');
    expect(patches.map((p) => p.body)).toEqual([{ agent_batch_min: 15 }]);
    expect((wrapper.get('.mapping-project-agent-batch-min').element as HTMLInputElement).value).toBe('15');
  });

  it('старое ядро без поля — 0; значение вне 0–60 не уходит', async () => {
    const { sent, fetchMock } = fakeFetch(() => [project()]);
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const input = wrapper.get('.mapping-project-agent-batch-min');
    expect((input.element as HTMLInputElement).value).toBe('0');
    for (const bad of ['61', '-1', '2.5']) {
      await input.setValue(bad);
      await flushPromises();
    }
    expect(sent.filter((s) => s.method === 'PATCH')).toEqual([]);
    expect((input.element as HTMLInputElement).value).toBe('0');
    expect(wrapper.text()).toContain('целое число минут от 0 до 60');
  });
});
