// Ф92-1 (A): итог агента владельца виден — «Агент за тебя» в ленте сессии и «ответил сессии» в итогах агента.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as client from '../src/api/client';
import type { SessionEvent } from '../src/api/types';
import OwnerResults from '../src/components/OwnerResults.vue';
import { stepView } from '../src/util/sessionSteps';

afterEach(() => vi.restoreAllMocks());

function say(summary: string, brief: SessionEvent['brief']): SessionEvent {
  return { id: 1, ts: 1, kind: 'say', tool: null, summary, is_error: false, ref: '1', brief } as SessionEvent;
}

describe('A1 лента сессии', () => {
  it('реплика агента подписана, реплика владельца — как прежде', () => {
    const a = stepView(say('владелец (next): дал доступ', { type: 'agent_say', by: 'agent' }));
    expect(a.text).toBe('Агент за тебя: дал доступ');
    expect(stepView(say('владелец (next): дал доступ', null)).text).toBe('ты написал: дал доступ');
  });
  it('с файлами подпись сохраняется', () => {
    const v = stepView(say('владелец (next): вот', { type: 'files', by: 'agent', files: [{ name: 'a.pdf', size: 1, kind: 'pdf' }] }));
    expect(v.text).toBe('Агент за тебя: вот\n📎 a.pdf');
  });
});

describe('A2 итоги агента', () => {
  it('последние строки за два часа видны сразу, у ответа — текст', async () => {
    const now = Date.now();
    vi.spyOn(client, 'getOwnerClosed').mockResolvedValue({
      items: [
        { id: 1, ref: 'turn:1:1', kind: 'turn', project_id: 5, closed_ts: now - 60_000, closed_by: 'agent', errand_result: null, agent_reply: { action: 'reply', text: 'дал доступ' } },
        { id: 2, ref: 'turn:1:2', kind: 'turn', project_id: 5, closed_ts: now - 120_000, closed_by: 'agent', errand_result: null, agent_reply: { action: 'ok', text: null } },
      ],
    });
    const w = mount(OwnerResults, { attachTo: document.body });
    await flushPromises();
    expect((w.get('details').element as HTMLDetailsElement).open).toBe(true);
    const rows = w.findAll('[data-test="owner-result"]');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.text()).toContain('ответил сессии: дал доступ');
    expect(rows[1]!.text()).toContain('принял без ответа');
    w.unmount();
  });
  it('старше двух часов — блок свёрнут', async () => {
    vi.spyOn(client, 'getOwnerClosed').mockResolvedValue({
      items: [{ id: 1, ref: 'turn:1:1', kind: 'turn', project_id: 5, closed_ts: Date.now() - 3 * 3600_000, closed_by: 'agent', errand_result: null, agent_reply: { action: 'ok', text: null } }],
    });
    const w = mount(OwnerResults, { attachTo: document.body });
    await flushPromises();
    expect((w.get('details').element as HTMLDetailsElement).open).toBe(false);
    w.unmount();
  });
  it('больше трёх — «ещё N»', async () => {
    const now = Date.now();
    vi.spyOn(client, 'getOwnerClosed').mockResolvedValue({
      items: [1, 2, 3, 4, 5].map((i) => ({ id: i, ref: `t${i}`, kind: 'turn', project_id: 5, closed_ts: now - i * 1000, closed_by: 'agent', errand_result: null, agent_reply: { action: 'ok' as const, text: null } })),
    });
    const w = mount(OwnerResults, { attachTo: document.body });
    await flushPromises();
    expect(w.findAll('[data-test="owner-result"]')).toHaveLength(3);
    await w.get('[data-test="owner-results-more"]').trigger('click');
    expect(w.findAll('[data-test="owner-result"]')).toHaveLength(5);
    w.unmount();
  });
});
