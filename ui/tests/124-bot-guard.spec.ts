// Ф63-2 (`docs/architecture.md`): пометка страховки у черновика и нажатия бота,
// выключатель `bot_chat` в панели «Автопилот».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AutopilotState, ButtonPress, OutboxListItem } from '../src/api/types';
import AutopilotPanel from '../src/components/AutopilotPanel.vue';
import DraftCard from '../src/components/DraftCard.vue';
import DraftRow from '../src/components/DraftRow.vue';
import PressCard from '../src/components/PressCard.vue';
import { resetAutopilot } from '../src/store/autopilot';
import { botGuardNote } from '../src/util/presses';

function item(over: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id: 5, channel_id: 1, project_id: 1, channel_title: 'Бот', text: 'Да', status: 'pending_approval',
    origin: 'api', error: null, attempts: 0, created_ts: 0, sent_ts: null, ext_id: null, ...over,
  };
}

function press(over: Partial<ButtonPress> = {}): ButtonPress {
  return {
    id: 1, account_id: 1, channel_id: 5, project_id: 3, message_id: 7, row: 1, col: 1, text: 'Продлить',
    by: 'session', role: 'менеджер', auto: false, auto_by: null, status: 'pending', reason: null,
    created_ts: 1, decided_ts: null, done_ts: null, updated_ts: 1, seq: 1, ...over,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetAutopilot();
});

describe('пометка страховки', () => {
  it('текст по категориям; чужое reason и нажатие без префикса — без пометки', () => {
    expect(botGuardNote('payment')).toBe('страховка: оплата — проверь перед отправкой');
    expect(botGuardNote('pdn')).toContain('личные данные');
    expect(botGuardNote('file')).toContain('файл');
    expect(botGuardNote('bot_guard:secret', true)).toContain('секрет');
    expect(botGuardNote('delete', true)).toBeNull();
    expect(botGuardNote('stale_button', true)).toBeNull();
    expect(botGuardNote(null)).toBeNull();
  });

  it('черновик: карточка и строка помечены, без bot_guard — нет', () => {
    for (const comp of [DraftCard, DraftRow]) {
      const w = mount(comp, { props: { item: item({ bot_guard: 'payment' }) } });
      expect(w.get('[data-bot-guard]').text()).toContain('оплата');
      w.unmount();
      const plain = mount(comp, { props: { item: item() } });
      expect(plain.find('[data-bot-guard]').exists()).toBe(false);
      plain.unmount();
    }
  });

  it('нажатие: «Нажать?» с reason bot_guard:<кат> помечено', () => {
    const w = mount(PressCard, { props: { press: press({ reason: 'bot_guard:delete' }) } });
    expect(w.get('[data-bot-guard]').text()).toContain('удаление');
    const other = mount(PressCard, { props: { press: press({ reason: 'flood' }) } });
    expect(other.find('[data-bot-guard]').exists()).toBe(false);
  });
});

describe('выключатель bot_chat', () => {
  it('панель «Автопилот» показывает его из ответа ядра с пояснением', async () => {
    const state: AutopilotState = {
      switches: [
        {
          key: 'bot_chat', group: 'Сообщения людям', title: 'переписка с ботами без черновика', manual: 'ask',
          meta: false, note: 'бот может пересылать твои сообщения людям (поддержка, обратная связь)',
        },
      ],
      global: { bot_chat: true },
      projects: {},
      local: true,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(state), { status: 200, headers: { 'content-type': 'application/json' } })),
    );
    const w = mount(AutopilotPanel);
    await flushPromises();
    const row = w.get('[data-switch="bot_chat"]');
    expect(row.text()).toContain('переписка с ботами');
    expect(row.text()).toContain('пересылать твои сообщения людям');
    w.unmount();
  });
});
