// Ф71-1 (docs/architecture.md, П11): движок сессий хаба Cursor в интерфейсе — подпись,
// модель и усилие (не ниже high), «продолжить руками», причина очереди, «Сжать» у Cursor нет.
import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it } from 'vitest';

import type { HubSession } from '../src/api/types';
import SessionHandoffForm from '../src/components/SessionHandoffForm.vue';
import SessionMenuItems from '../src/components/SessionMenuItems.vue';
import { engineLabel } from '../src/store/peerMsgs';
import { ENGINE_LABEL, ENGINE_SHORT } from '../src/util/engine';
import { sayQueueReason } from '../src/util/sayQueue';
import {
  SESSION_EFFORTS_BY_ENGINE,
  SESSION_MODEL_HINTS,
  effortChoices,
  effortForEngine,
  sessionTerminalCommand,
} from '../src/util/sessions';

const NOW = new Date(2026, 9, 19, 12, 0, 0).getTime();

function session(over: Partial<HubSession> = {}): HubSession {
  return {
    id: 5, project_id: 3, role: 'программист', role_no: 1, engine: 'cursor', model: null, effort: null, mode: 'manual',
    title: null, state: 'idle', host_seen_ts: NOW, created_ts: NOW, updated_ts: NOW, closed_ts: null, close_reason: null,
    created_by: 'ui', seq: 10, last_event_id: null, last_turn_ts: null, failure: null, wait_reason: null, pending_requests: 0,
    queued_says: 0, interrupt_requested: false, sleep_requested: false, sub_id: null, listening: false, engine_session_id: null,
    workdir: '/p', cache: null, usage: null, ...over,
  } as HubSession;
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('Ф71-1: Cursor в интерфейсе', () => {
  it('подписи, модель по умолчанию и усилие только high и xhigh', () => {
    expect(ENGINE_LABEL.cursor).toBe('Cursor');
    expect(ENGINE_SHORT.cursor).toBe('Cursor');
    expect(engineLabel('cursor')).toBe('Cursor');
    expect(SESSION_MODEL_HINTS.cursor).toEqual(['grok-4.7', 'claude-sonnet-5-5', 'claude-opus-5-5']);
    expect(SESSION_EFFORTS_BY_ENGINE.cursor).toEqual(['high', 'xhigh']);
    expect(effortChoices('cursor')).toEqual(['high', 'xhigh']);
    expect(effortChoices('cursor', 'low')).toEqual(['high', 'xhigh', 'low']); // старые данные — видны как есть
    expect(effortForEngine('cursor', 'medium')).toBe(''); // смена движка в форме: «по умолчанию» (хост — high)
    expect(effortForEngine('cursor', 'xhigh')).toBe('xhigh');
  });

  it('продолжить руками — cursor-agent --resume <chatId>', () => {
    const asleep = session({ state: 'asleep', engine_session_id: 'c-1e2' });
    expect(sessionTerminalCommand(asleep)).toBe('cd /p && cursor-agent --resume c-1e2');
    expect(sessionTerminalCommand({ ...asleep, engine: 'opencode' })).toBe('cd /p && opencode -s c-1e2');
    expect(sessionTerminalCommand({ ...asleep, engine: 'claude' })).toBe('cd /p && claude --resume c-1e2');
  });

  it('очередь во время хода: Cursor берёт посреди хода (Ф71-1 6б; Ф74-1 — по `mid_turn` профиля)', () => {
    const says = [{ status: 'queued' as const }];
    // Ф74-8: Cursor берёт её только на ближайшем вызове инструмента (`mid_turn_tool_only`).
    expect(sayQueueReason(session({ state: 'running' }), [], says)).toBe(
      'возьмёт при следующем вызове инструмента, иначе — следующим ходом',
    );
    expect(sayQueueReason(session({ state: 'running', engine: 'claude' }), [], says)).toBe(
      'сессия возьмёт после текущего шага',
    );
  });

  it('меню «⋯»: у Cursor нет «Сжать» и «Сжатие…», у Claude есть', () => {
    const cursor = mount(SessionMenuItems, { props: { win: null, session: session() } });
    expect(cursor.find('.session-menu-compact').exists()).toBe(false);
    expect(cursor.find('.session-menu-compact-settings').exists()).toBe(false);
    expect(cursor.find('.session-menu-model').exists()).toBe(true);
    const claude = mount(SessionMenuItems, { props: { win: null, session: session({ engine: 'claude' }) } });
    expect(claude.find('.session-menu-compact').exists()).toBe(true);
  });

  it('«Новая с передачей дел»: Cursor в списке движков', () => {
    const w = mount(SessionHandoffForm, { props: { session: session() } });
    const options = w.findAll('.session-handoff-engine option').map((o) => o.attributes('value'));
    expect(options).toEqual(['claude', 'opencode', 'cursor']);
  });
});
