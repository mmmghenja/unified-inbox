// Ф41-2а (`docs/architecture.md`): подписка сессии хаба в блоке «Агенты» — подпись состояния
// («спит» / «в работе» / «ждёт входа») вместо «не слушает»; в подписи «кто слушает беседу» — номер сессии хаба.
import { enableAutoUnmount, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { ChanSub, ProjectSub } from '../src/api/types';
import AgentsBlock from '../src/components/AgentsBlock.vue';
import { store } from '../src/store/instance';
import { listeningLabel } from '../src/store/peerMsgs';
import { listenerName, listeningLabelOf } from '../src/util/listeners';

enableAutoUnmount(afterEach);

function sub(overrides: Partial<ProjectSub> = {}): ProjectSub {
  return {
    id: 31,
    project_id: 3,
    role: 'маркетолог',
    role_no: 1,
    mode: 'cards',
    engine: 'claude',
    session_name: null,
    live: true,
    listening: false,
    waiting: false,
    ...overrides,
  };
}

function chanSub(overrides: Partial<ChanSub> = {}): ChanSub {
  return {
    id: 1,
    channel_id: 5,
    project_id: 3,
    session_name: null,
    session_pid: 4242,
    live: true,
    listening: false,
    waiting: false,
    ended_ts: null,
    ...overrides,
  };
}

beforeEach(() => {
  store.state.peerMsgs.agentSubs = [];
  store.state.projects = [
    { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: null, unread: 0 },
  ];
});

describe('Ф41-2а: подпись состояния сессии хаба', () => {
  it('listeningLabel: у внешней — слушает / не слушает, у сессии хаба — состояние записи', () => {
    expect(listeningLabel(sub({ listening: true }))).toBe('слушает');
    expect(listeningLabel(sub({ listening: false }))).toBe('не слушает');
    expect(listeningLabel(sub({ held: true, state: 'asleep' }))).toBe('спит');
    expect(listeningLabel(sub({ held: true, state: 'running' }))).toBe('в работе');
    expect(listeningLabel(sub({ held: true, state: 'idle' }))).toBe('ждёт входа');
    // Состояние не пришло (ядро старше UI) — сессия хаба без процесса: «спит», а не «не слушает».
    expect(listeningLabel(sub({ held: true }))).toBe('спит');
  });

  it('блок «Агенты»: у подписки сессии хаба — состояние вместо «не слушает»', () => {
    store.state.peerMsgs.agentSubs = [
      sub({ id: 1, held: true, hub_session_id: 7, state: 'asleep', engine: 'opencode' }),
      sub({ id: 2, role_no: 2, held: true, hub_session_id: 8, state: 'running' }),
      sub({ id: 3, role_no: 3, held: true, hub_session_id: 9, state: 'idle' }),
      sub({ id: 4, role_no: 4 }),
    ];
    const rows = mount(AgentsBlock)
      .findAll('.agent-row')
      .map((r) => r.text());
    expect(rows[0]).toContain('OpenCode');
    expect(rows[0]).toContain('спит');
    expect(rows[1]).toContain('в работе');
    expect(rows[2]).toContain('ждёт входа');
    for (const row of rows.slice(0, 3)) expect(row).not.toContain('не слушает');
    expect(rows[3]).toContain('не слушает');
  });

  it('кто слушает беседу: сессия хаба без роли — по номеру записи, не по условному pid', () => {
    const held = chanSub({ session_pid: -7, held: true, hub_session_id: 7, state: 'asleep' });
    expect(listenerName(held)).toBe('сессия хаба #7');
    expect(listenerName(chanSub({ ...held, role: 'маркетолог', role_no: 1 }))).toBe('маркетолог');
    expect(listenerName(chanSub())).toBe('#4242');
    // Спящая сессия хаба жива (`live: true`) — беседу слушает.
    expect(listeningLabelOf([held], 5)).toBe('слушает: сессия хаба #7');
  });
});
