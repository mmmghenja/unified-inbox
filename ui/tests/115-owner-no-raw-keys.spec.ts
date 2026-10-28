// Приёмка Ф56, круг 3: в карточке очереди владельца нет сырых служебных ключей и чисел времени — у каждого вида
// подписанные по-русски поля. Каждая карточка фикстуры (с добавленными служебными полями настоящего ядра)
// отрисована, развёрнута внутри и открыта окном; запасной рендер неизвестного вида — только строки
// неслужебных ключей.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { mount } from '@vue/test-utils';
import { describe, expect, it } from 'vitest';

import type { OwnerItem, OwnerQueue } from '../src/api/types';
import OwnerCard from '../src/components/OwnerCard.vue';
import OwnerFullBody from '../src/components/OwnerFullBody.vue';
import { fullFields, isServiceKey } from '../src/util/owner_channel';

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const NOW = 1760360000000;

// Служебное, которое ядро кладёт в `full` разных видов (записи БД целиком).
const SERVICE: Record<string, unknown> = {
  seq: 4242,
  status: 'pending',
  created_ts: 1760359000000,
  updated_ts: 1760359100000,
  decided_ts: null,
  expires_ts: 1760360480000,
  session_id: 11,
  project_id: 3,
  account_id: 8,
  channel_id: 1099511627837,
  message_id: 77001,
  origin: 'mcp',
  attempts: 0,
  row: 0,
  col: 1,
  role_no: 2,
  token_name: 'hub-all',
  client_name: 'claude-code',
  input_skipped: 0,
  input_truncated: false,
  can_decline: true,
};
const RAW = /\b(id|kind|seq|status|state|origin|attempts|cut|turns|by_ui_say|wait_reason|role_no|token_name|client_name|can_decline|input_skipped|input_truncated|[a-z]+_ts|[a-z]+_id)\b|\b1760\d{9}\b|4242|77001|1099511627837|\[object|\{"|true|false|null/;

function withService(item: OwnerItem): OwnerItem {
  const copy = JSON.parse(JSON.stringify(item)) as OwnerItem;
  for (const [key, value] of Object.entries(SERVICE)) {
    if (!(key in copy.full)) copy.full[key] = value;
  }
  return copy;
}

describe('56-3: карточка без сырых ключей', () => {
  const items = FIXTURE.items.map(withService);

  it('в фикстуре есть все виды очереди', () => {
    const kinds = new Set(items.map((i) => i.kind));
    for (const kind of ['permission', 'question', 'human', 'draft', 'press', 'join', 'leave', 'rule', 'agent_request', 'turn', 'stall', 'broken', 'dead', 'stuck', 'agent_msg']) {
      expect(kinds.has(kind), kind).toBe(true);
    }
  });

  for (const item of items) {
    it(`${item.kind} ${item.ref}: карточка, раскрытая карточка и окно — без служебного`, async () => {
      const w = mount(OwnerCard, { props: { item, nowMs: NOW, agentOnline: true }, attachTo: document.body });
      const expand = w.find('.owner-expand');
      if (expand.exists()) await expand.trigger('click');
      const shown = w.text().replace(item.brief, '');
      expect(shown, shown).not.toMatch(RAW);
      const body = mount(OwnerFullBody, { props: { item, nowMs: NOW } });
      // Тексты людей и модели (ответ, команда, черновик) — не поля: проверяются только подписи и значения полей.
      for (const pair of body.findAll('[data-test="owner-field"], .owner-pair')) {
        expect(pair.text(), pair.text()).not.toMatch(RAW);
      }
      for (const key of body.findAll('.owner-key')) expect(key.text()).toMatch(/^[а-яё ]+:$/i);
      body.unmount();
      w.unmount();
    });
  }

  it('вопрос показан один раз', () => {
    const item = items.find((i) => i.kind === 'question')!;
    const w = mount(OwnerCard, { props: { item, nowMs: NOW, agentOnline: true } });
    expect(w.text().split('Какой тариф выбрать?').length - 1).toBe(1);
    w.unmount();
  });

  it('время — словами: «до ЧЧ:ММ» в шапке, «N мин назад» в полях', async () => {
    const stuck = items.find((i) => i.kind === 'stuck')!;
    const w = mount(OwnerCard, { props: { item: stuck, nowMs: NOW, agentOnline: true } });
    await w.get('.owner-expand').trigger('click');
    expect(w.get('.owner-full').text()).toContain('30 мин назад');
    w.unmount();
    const human = items.find((i) => i.kind === 'human')!;
    const h = mount(OwnerCard, { props: { item: human, nowMs: NOW, agentOnline: true } });
    expect(h.get('.owner-until').text()).toMatch(/^до \d\d:\d\d$/);
    expect(h.text()).toContain('что: капча');
    h.unmount();
  });

  it('неизвестный вид: только строки неслужебных ключей', () => {
    const item = withService({ ...items[0], kind: 'newkind', brief: 'новое', full: { note: 'текст заметки', count: 5, extra_id: 9, deep: { a: 1 }, wait_reason: 'limit' } } as OwnerItem);
    expect(fullFields(item, NOW)).toEqual([{ key: 'note', label: 'note', value: 'текст заметки' }]);
    expect(isServiceKey('expires_ts') && isServiceKey('kind') && !isServiceKey('note')).toBe(true);
  });
});
