// Ф99-7.2 (docs/architecture.md): «Всегда в этой сессии» — кнопка на карточке разрешения, когда ядро назвало
// ключ (`request.always`); «⋯ → Разрешения…» — список с «×».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { HubSession, SessionRequest } from '../src/api/types';
import SessionAlwaysModal from '../src/components/SessionAlwaysModal.vue';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import WorkRequestCard from '../src/components/WorkRequestCard.vue';
import { store } from '../src/store/instance';
import { alwaysLabel } from '../src/util/sessionAlways';
import type { SessionWindowHandle } from '../src/util/sessionHandle';

const SLACK = 'mcp__claude_ai_Slack__slack_send_message';

function request(over: Partial<SessionRequest> = {}): SessionRequest {
  return {
    id: 7, session_id: 5, project_id: 3, kind: 'permission', tool: SLACK, input: { text: 'x' }, input_truncated: false,
    reason: 'под кнопкой', status: 'pending', answer: null, created_ts: 1, decided_ts: null, delivered: false, seq: 1,
    rule: 'mcp_write', always: `mcp:${SLACK}`, ...over,
  } as SessionRequest;
}

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'claude', model: null, effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: 1, created_ts: 1, updated_ts: 1, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

function handle(): SessionWindowHandle {
  return {
    busy: false, canTerminal: false, nothingToSleep: false,
    sleep: vi.fn(), wake: vi.fn(), compact: vi.fn(), mcpReload: vi.fn(async () => {}), returnMain: vi.fn(async () => {}), close: vi.fn(),
    openPanel: vi.fn(), openRoleText: vi.fn(), openCompact: vi.fn(), openAlways: vi.fn(), openTerminal: vi.fn(), toggleSubEdit: vi.fn(),
    chooseMode: vi.fn(async () => {}), isAuto: false,
  };
}

afterEach(() => {
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});

describe('карточка разрешения: «Всегда в этой сессии»', () => {
  it('ключ назван ядром — кнопка рядом с «Разрешить», решение уходит с `always: true`', async () => {
    const decide = vi.fn(async () => 'ok' as const);
    const w = mount(WorkRequestCard, { props: { request: request(), decide, retry: async () => 'ok', dismiss: () => undefined } });
    const button = w.get('[data-test="request-always"]');
    expect(button.text()).toBe('Всегда в этой сессии');
    expect(button.attributes('title')).toContain(SLACK);
    await button.trigger('click');
    expect(decide).toHaveBeenCalledWith({ decision: 'allow', always: true });
    await w.get('button.work-allow').trigger('click');
    expect(decide).toHaveBeenLastCalledWith({ decision: 'allow' }); // обычное «Разрешить» — без «всегда»
  });

  it('нет ключа (push, вопрос) — кнопки нет', () => {
    const props = { decide: async () => 'ok' as const, retry: async () => 'ok' as const, dismiss: () => undefined };
    const push = mount(WorkRequestCard, { props: { ...props, request: request({ tool: 'Bash', rule: 'push', always: null }) } });
    expect(push.find('[data-test="request-always"]').exists()).toBe(false);
    const question = mount(WorkRequestCard, {
      props: { ...props, request: request({ kind: 'question', tool: 'AskUserQuestion', always: 'mcp:x' }) },
    });
    expect(question.find('[data-test="request-always"]').exists()).toBe(false);
  });
});

describe('«⋯ → Разрешения…»', () => {
  it('пункт меню открывает окно и показывает число', async () => {
    const win = handle();
    const w = mount(SessionMenuItems, {
      props: { win, session: session({ always: [{ key: 'dir:/srv/out', ts: 1 }] }) },
    });
    const item = w.get('.session-menu-always');
    expect(item.text().replace(/\s+/g, ' ')).toBe('Разрешения… (1)');
    await item.trigger('click');
    expect(win.openAlways).toHaveBeenCalledTimes(1);
  });

  it('окно: список подписями, «×» снимает ключ; пусто — «Пусто.»', async () => {
    const remove = vi.spyOn(store, 'removeSessionAlways').mockResolvedValue({ ok: true });
    const items = [
      { key: `mcp:${SLACK}`, ts: 1 },
      { key: 'dir:/srv/out', ts: 2 },
    ];
    const w = mount(SessionAlwaysModal, { props: { session: session({ always: items }) }, attachTo: document.body });
    const rows = document.querySelectorAll('.session-always-item');
    expect([...rows].map((r) => r.querySelector('.session-always-label')?.textContent)).toEqual([
      alwaysLabel(`mcp:${SLACK}`),
      'запись в каталог /srv/out',
    ]);
    (rows[1].querySelector('.session-always-remove') as HTMLButtonElement).click();
    await flushPromises();
    expect(remove).toHaveBeenCalledWith(5, 'dir:/srv/out');
    w.unmount();
    const empty = mount(SessionAlwaysModal, { props: { session: session() }, attachTo: document.body });
    expect(document.querySelector('.session-always-empty')?.textContent?.trim()).toBe('Пусто.');
    empty.unmount();
  });
});
