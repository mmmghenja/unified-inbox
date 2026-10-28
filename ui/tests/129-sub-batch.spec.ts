// Ф67-1 (`docs/architecture.md`): интервал пачки сигналов подписки в строке подписки сессии хаба
// («подписка ▾») и в списке подписок («Агенты») — «пачкой раз в N мин», только если N > 0.
import { describe, expect, it } from 'vitest';

import type { ProjectSub, SessionSub } from '../src/api/types';
import { modeLabel } from '../src/store/peerMsgs';
import { batchLabel, buildWantSub, emptySubForm, subSummary } from '../src/util/sessionSub';

function projectSub(overrides: Partial<ProjectSub> = {}): ProjectSub {
  return {
    id: 31,
    project_id: 5,
    role: 'менеджер',
    role_no: 1,
    mode: 'messages',
    msg_filter: 'all',
    engine: 'claude',
    session_name: null,
    live: true,
    listening: true,
    waiting: true,
    ...overrides,
  };
}

function sessionSub(overrides: Partial<SessionSub> = {}): SessionSub {
  return {
    id: 7,
    mode: 'messages',
    msg_filter: 'all',
    keywords: [],
    exclude: [],
    batch_min: null,
    accounts: null,
    pending: 2,
    ...overrides,
  };
}

describe('Ф67-1: интервал пачки подписки', () => {
  it('batchLabel — только целое больше нуля', () => {
    expect(batchLabel(10)).toBe('пачкой раз в 10 мин');
    expect(batchLabel(0)).toBe('');
    expect(batchLabel(null)).toBe('');
    expect(batchLabel(undefined)).toBe('');
    expect(batchLabel(1.5)).toBe('');
  });

  it('строка подписки сессии хаба называет интервал', () => {
    expect(subSummary(sessionSub({ batch_min: 10 }))).toBe('подписка #7 · сообщения · пачкой раз в 10 мин · ждёт сигналов: 2');
    expect(subSummary(sessionSub({ keywords: ['цена'], batch_min: 15 }))).toBe(
      'подписка #7 · сообщения · ключи: цена · пачкой раз в 15 мин · ждёт сигналов: 2',
    );
    expect(subSummary(sessionSub({ batch_min: 0 }))).toBe('подписка #7 · сообщения · ждёт сигналов: 2');
    expect(subSummary(sessionSub())).toBe('подписка #7 · сообщения · ждёт сигналов: 2');
  });

  it('список подписок: режим, пачка, аккаунты', () => {
    expect(modeLabel(projectSub({ batch_min: 5 }))).toBe('каждое сообщение · пачкой раз в 5 мин');
    expect(modeLabel(projectSub({ batch_min: 5, accounts: { only: [8] } }))).toBe(
      'каждое сообщение · пачкой раз в 5 мин · только аккаунты 8',
    );
    expect(modeLabel(projectSub({ mode: 'cards', batch_min: 10 }))).toBe('карточки · пачкой раз в 10 мин');
    expect(modeLabel(projectSub({ batch_min: 0 }))).toBe('каждое сообщение');
    expect(modeLabel(projectSub())).toBe('каждое сообщение');
    expect(modeLabel(projectSub({ mode: 'direct', batch_min: 5 }))).toBe('только сообщения агентов');
  });

  it('интервал задаётся тем же полем формы и без ключей', () => {
    const form = { ...emptySubForm('messages'), batch: '10' };
    const built = buildWantSub(form);
    expect(built.ok).toBe(true);
    expect(built.ok && built.value?.batch_min).toBe(10);
    expect(built.ok && built.value?.keywords).toEqual([]);
  });
});
