// Номер черновика: сессии пишут «черновик #N» — этот же `#id` виден в шапке карточки
// черновика (главная и закреплённая над полем ввода) и в строке ленты «черновик #N · ждёт подтверждения».
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { OutboxListItem } from '../src/api/types';
import DraftCard from '../src/components/DraftCard.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import { store } from '../src/store/instance';

function draft(id: number): OutboxListItem {
  return {
    id,
    channel_id: 7,
    project_id: 3,
    channel_title: 'Клиент',
    text: 'Добрый день',
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: 0,
    sent_ts: null,
    ext_id: null,
    project_name: 'Umbrella',
    source: 'telegram',
    kind: 'dm',
    author: 'mcp:all',
    reply_to: null,
    thread: null,
    paced: false,
    not_before_ts: null,
    account_id: 1,
    files: [],
  };
}

beforeEach(() => {
  api.setCsrf('c');
  store.state.draftSelection = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('номер черновика', () => {
  it('в шапке карточки на главной — #id', () => {
    const wrapper = mount(DraftCard, { props: { item: draft(1099511628123) } });
    expect(wrapper.get('.draft-card-head .draft-card-num').text()).toBe('#1099511628123');
  });

  it('в закреплённой (compact) карточке беседы — тоже', () => {
    const wrapper = mount(DraftCard, { props: { item: draft(57), compact: true, showAddress: false } });
    expect(wrapper.get('.draft-card-num').text()).toBe('#57');
  });

  it('в строке ленты — «черновик #N · ждёт подтверждения»', () => {
    const wrapper = mount(OutboxStatusBadge, {
      props: { outboxId: 57, status: 'pending_approval', text: 'Добрый день', paced: false, notBeforeTs: null },
    });
    expect(wrapper.get('.outbox-label').text()).toBe('черновик #57 · ждёт подтверждения');
  });
});
