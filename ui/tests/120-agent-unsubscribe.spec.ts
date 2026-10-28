// «Отписать от хаба» у внешней сессии: кнопка только при живой подписке; подтверждение -> POST /subs/<id>/end с {}.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import BoardAgent from '../src/components/BoardAgent.vue';
import type { AgentLine } from '../src/store/peerMsgs';

const calls: { url: string; method: string; body: string | null }[] = [];

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method ?? 'GET', body: (init?.body as string | undefined) ?? null });
      return new Response(JSON.stringify({ sub: { id: 7 } }), { status: 200, headers: { 'content-type': 'application/json' } });
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function line(sub: unknown): AgentLine {
  return {
    key: 's7',
    projectId: 1,
    name: 'тестер',
    hasRole: true,
    address: { project_id: 1, role: 'тестер', number: 1 },
    sub: sub as AgentLine['sub'],
    cwd: null,
    unread: 0,
  };
}
const item = { kind: 'agent', id: 's7' } as never;
const stubs = { AgentDialog: true, BoardColumnShell: { template: '<div><slot /></div>' } };

describe('отписать от хаба', () => {
  it('без подписки кнопки нет', () => {
    const w = mount(BoardAgent, { props: { item, line: line(null) }, global: { stubs } });
    expect(w.find('[data-test="agent-unsubscribe"]').exists()).toBe(false);
  });

  it('с подпиской: отказ в подтверждении — запроса нет; согласие — POST /subs/7/end с {}', async () => {
    const sub = { id: 7, project_id: 1, role: 'тестер', role_no: 1, engine: 'opencode', listening: true };
    const w = mount(BoardAgent, { props: { item, line: line(sub) }, global: { stubs } });
    const btn = w.find('[data-test="agent-unsubscribe"]');
    expect(btn.exists()).toBe(true);

    vi.spyOn(window, 'confirm').mockReturnValue(false);
    await btn.trigger('click');
    await flushPromises();
    expect(calls.filter((c) => c.url.includes('/subs/7/end'))).toHaveLength(0);

    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await btn.trigger('click');
    await flushPromises();
    const post = calls.find((c) => c.url.includes('/subs/7/end'));
    expect(post?.method).toBe('POST');
    expect(JSON.parse(post?.body ?? 'null')).toEqual({});
  });
});
