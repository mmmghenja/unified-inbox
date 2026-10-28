// Владелец 07.10: «черновики — я номеров не вижу в интерфейсе». Номер (`id` строки outbox, его называют `hub_draft`,
// `hub_draft_new`, `hub_drafts`) виден тихой подписью у черновика в строке, у сообщения в очереди, в панели очереди
// и в «Отправлено сегодня»; поле «№» оставляет строки с такими цифрами.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OutboxListItem } from '../src/api/types';
import DraftRow from '../src/components/DraftRow.vue';
import NumberSearch from '../src/components/NumberSearch.vue';
import OutboxQueuePanel from '../src/components/OutboxQueuePanel.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import { filterByNumber, numberQuery } from '../src/util/draftNumber';

function item(id: number, over: Partial<OutboxListItem> = {}): OutboxListItem {
  return {
    id, channel_id: 1, project_id: 1, channel_title: `Беседа ${id}`, text: `текст ${id}`, status: 'pending_approval',
    origin: 'api', error: null, attempts: 0, created_ts: 0, sent_ts: null, ext_id: null, ...over,
  };
}

afterEach(() => vi.restoreAllMocks());

describe('поиск по номеру', () => {
  it('цифры из «#998», «№ 998», «998»; пусто — всё; не цифры — всё', () => {
    expect(numberQuery('№ 998')).toBe('998');
    expect(numberQuery('#998')).toBe('998');
    const rows = [item(998), item(1099511628774), item(12)];
    expect(filterByNumber(rows, '998').map((r) => r.id)).toEqual([998]);
    expect(filterByNumber(rows, '#28774').map((r) => r.id)).toEqual([1099511628774]);
    expect(filterByNumber(rows, '').length).toBe(3);
    expect(filterByNumber(rows, 'abc').length).toBe(3);
    expect(filterByNumber(rows, '55').length).toBe(0);
    expect(filterByNumber([item(6), item(1099511627776)], '6').map((r) => r.id)).toEqual([6]);
  });

  it('поле «№» меняет значение и сбрасывается крестиком', async () => {
    const w = mount(NumberSearch, { props: { modelValue: '' } });
    await w.get('input').setValue('998');
    expect(w.emitted('update:modelValue')?.at(-1)).toEqual(['998']);
    const filled = mount(NumberSearch, { props: { modelValue: '12' } });
    await filled.get('.number-search-clear').trigger('click');
    expect(filled.emitted('update:modelValue')?.at(-1)).toEqual(['']);
  });
});

describe('номер в интерфейсе', () => {
  it('строка черновика: #id текстом рядом с адресатом', () => {
    const w = mount(DraftRow, { props: { item: item(998) } });
    expect(w.get('.draft-card-num').text()).toBe('#998');
  });

  it('сообщение в очереди с временем: номер перед временем; во всех состояниях до доставки', () => {
    const base = { outboxId: 998, channelId: 1 };
    const states = ['queued', 'sending', 'unknown', 'failed', 'cancelled'] as const;
    for (const status of states) {
      const w = mount(OutboxStatusBadge, { props: { ...base, status, notBeforeTs: Date.now() + 3_600_000, paced: true } as never });
      expect(w.text(), status).toContain('#998');
    }
  });

  it('доставленное сообщение в ленте — без номера: он был бы под каждым своим сообщением', () => {
    const w = mount(OutboxStatusBadge, { props: { outboxId: 998, channelId: 1, status: 'sent' } as never });
    expect(w.text()).toContain('доставлено');
    expect(w.text()).not.toContain('#998');
  });

  it('в поле «№» помещается номер боевой беседы целиком (13 цифр и «#»)', () => {
    const w = mount(NumberSearch, { props: { modelValue: '' } });
    expect(Number(w.get('input').attributes('maxlength'))).toBeGreaterThanOrEqual(14);
  });

  it('панель очереди: номер у строки; поле «№» оставляет нужную', async () => {
    vi.spyOn(api, 'listOutbox').mockResolvedValue([item(998), item(1001), item(5)]);
    const w = mount(OutboxQueuePanel, { props: { status: 'pending_approval' } });
    await flushPromises();
    expect(w.findAll('.queue-item .draft-card-num').map((n) => n.text())).toEqual(['#998', '#1001', '#5']);
    await w.get('.number-search-input').setValue('998');
    expect(w.findAll('.queue-item').length).toBe(1);
    expect(w.get('.queue-item').text()).toContain('#998');
    await w.get('.number-search-input').setValue('7777');
    expect(w.find('.number-search-empty').exists()).toBe(true);
  });
});
