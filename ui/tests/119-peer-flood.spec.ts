// Ф69-1 E: строка «Стоп» у аккаунта и «Снять стоп» (подтверждение, вызов, 409); причина в бейдже очереди.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import TopMenu from '../src/components/TopMenu.vue';
import { store } from '../src/store/instance';

function account(over: Record<string, unknown>) {
  return {
    id: 1, source: 'telegram', title: 'Umbrella', status: 'ok', last_ok_ts: 1, last_error: null, flood_until: null,
    peer_flood_ts: null, ...over,
  };
}

function setAccounts(list: unknown[]): void {
  (store.state as unknown as { status: unknown }).status = { accounts: list };
}

beforeEach(() => {
  vi.spyOn(store, 'refreshStatus').mockResolvedValue();
});
afterEach(() => vi.restoreAllMocks());

describe('Ф69-1: стоп PeerFlood в строке статусов', () => {
  it('стоп новых людей: строка с временем и кнопка', () => {
    setAccounts([account({ peer_flood_ts: new Date(2026, 9, 16, 14, 5).getTime() })]);
    const w = mount(TopMenu);
    expect(w.text()).toContain('Стоп: первые сообщения новым людям с 14:05');
    expect(w.find('.account-stop-resume').exists()).toBe(true);
  });

  it('limited + PeerFlood: стоп всей отправки', () => {
    setAccounts([account({ status: 'limited', last_error: 'PeerFlood' })]);
    expect(mount(TopMenu).text()).toContain('Стоп: вся отправка (PeerFlood)');
  });

  it('без стопа строки нет', () => {
    setAccounts([account({})]);
    expect(mount(TopMenu).find('.account-stop').exists()).toBe(false);
  });

  it('отказ в подтверждении — вызова нет; согласие — вызов и обновление статуса', async () => {
    setAccounts([account({ peer_flood_ts: 1000 })]);
    const resume = vi.spyOn(api, 'resumeAccount').mockResolvedValue({ ok: true, cleared: ['peer_flood'] });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const w = mount(TopMenu);
    await w.find('.account-stop-resume').trigger('click');
    expect(resume).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await w.find('.account-stop-resume').trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenLastCalledWith(expect.stringContaining('@SpamBot'));
    expect(confirm).toHaveBeenLastCalledWith(expect.stringContaining('Umbrella'));
    expect(resume).toHaveBeenCalledWith(1);
    expect(store.refreshStatus).toHaveBeenCalled();
  });

  it('409 — тихо обновляет статус', async () => {
    setAccounts([account({ peer_flood_ts: 1000 })]);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(api, 'resumeAccount').mockRejectedValue(new api.ApiError(409, 'nothing_to_resume', null));
    const w = mount(TopMenu);
    await w.find('.account-stop-resume').trigger('click');
    await flushPromises();
    expect(store.refreshStatus).toHaveBeenCalled();
  });
});

describe('Ф69-1: причина в бейдже очереди', () => {
  it('queued + held — причина рядом с номером', () => {
    const w = mount(OutboxStatusBadge, { props: { outboxId: 7, status: 'queued', text: 'x', held: 'peer_flood' } });
    expect(w.text()).toContain('#7');
    expect(w.text()).toContain('ждёт: стоп первых сообщений новым людям');
  });

  it('queued по графику + held — причина тоже', () => {
    const w = mount(OutboxStatusBadge, {
      props: { outboxId: 7, status: 'queued', text: 'x', paced: true, notBeforeTs: Date.now() + 3_600_000, held: 'peer_flood' },
    });
    expect(w.text()).toContain('ждёт: стоп первых сообщений новым людям');
  });

  it('без held причины нет', () => {
    const w = mount(OutboxStatusBadge, { props: { outboxId: 7, status: 'queued', text: 'x' } });
    expect(w.text()).not.toContain('стоп');
  });
});
