// Пункт 8 спеки: XSS-набор из плана — в тексте сообщения, entity-ссылке, названии канала, имени
// отправителя, названии проекта и причине ошибки outbox. Проверяется РЕАЛЬНЫЙ смонтированный DOM.
//
// Мутационная самопроверка (см. отчёт исполнителя 6b): при временной замене `wrap()`/`safeHref()` в
// `src/render/renderText.ts` на пропуск проверки схемы этот файл краснеет на тестах группы «entity-ссылка» —
// проверено руками, возвращено обратно.
import { mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Message, OutboxItem, OutboxListItem } from '../src/api/types';
import ChannelView from '../src/components/ChannelView.vue';
import MessageRow from '../src/components/MessageRow.vue';
import MessageText from '../src/components/MessageText';
import OutboxQueuePanel from '../src/components/OutboxQueuePanel.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';

const XSS_PAYLOADS = [
  '<img src=x onerror=alert(1)>',
  '<svg><script>alert(1)</script>',
  'javascript:alert(1)',
  '"><script>alert(1)</script>',
  'JaVaScRiPt:alert(1)',
  'data:text/html,<script>alert(1)</script>',
  'vbscript:msgbox(1)',
  ' javascript:alert(1)',
];

const ALLOWED_TAGS = new Set(['SPAN', 'STRONG', 'EM', 'CODE', 'PRE', 'A']);

function assertWhitelistOnly(root: Element, expectedText: string): void {
  const elements = [root, ...root.querySelectorAll('*')];
  for (const el of elements) {
    expect(ALLOWED_TAGS.has(el.tagName)).toBe(true);
    for (const attr of Array.from(el.attributes)) {
      expect(attr.name.toLowerCase().startsWith('on')).toBe(false);
    }
    if (el.tagName === 'A') {
      const href = el.getAttribute('href') ?? '';
      expect(/^(https?:|mailto:)/i.test(href)).toBe(true);
    }
  }
  expect(root.textContent).toBe(expectedText);
}

function assertNoForeignElements(root: Element): void {
  expect(root.querySelectorAll('img, svg, script, iframe, object, embed').length).toBe(0);
  for (const el of root.querySelectorAll('*')) {
    for (const attr of Array.from(el.attributes)) {
      expect(attr.name.toLowerCase().startsWith('on')).toBe(false);
    }
    if (el.tagName === 'A') {
      const href = el.getAttribute('href') ?? '';
      expect(/^(https?:|mailto:)/i.test(href)).toBe(true);
    }
  }
}

function baseMessage(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
    project_id: 1,
    ext_id: '1',
    thread_ext_id: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'x',
    entities_json: null,
    attachments_json: null,
    forward_json: null,
    mentions_me: false,
    refs_json: null,
    reactions_json: null,
    ts: 1,
    edited_ts: null,
    deleted: false,
    outbox_id: null,
    outbox_status: null,
    sender: null,
    ...overrides,
  };
}

describe('08 XSS: текст сообщения', () => {
  it.each(XSS_PAYLOADS)('payload %s рендерится только текстом', (payload) => {
    const wrapper = mount(MessageText, { props: { text: payload, entitiesJson: null } });
    assertWhitelistOnly(wrapper.element, payload);
  });
});

describe('08 XSS: entity-ссылка (offset/length корректны, url — атака)', () => {
  it.each(XSS_PAYLOADS)('payload %s как url ссылки не даёт <a>, текст цел', (payload) => {
    const text = 'нажми сюда';
    const entities = JSON.stringify([{ type: 'link', offset: 0, length: text.length, url: payload }]);
    const wrapper = mount(MessageText, { props: { text, entitiesJson: entities } });
    assertWhitelistOnly(wrapper.element, text);
    expect(wrapper.element.querySelector('a')).toBeNull();
  });
});

describe('08 XSS: имя отправителя (MessageRow, обычная text-интерполяция Vue)', () => {
  it.each(XSS_PAYLOADS)('payload %s в имени отправителя — просто текст', (payload) => {
    const message = baseMessage({ sender: { id: 1, name: payload, handle: null } });
    const wrapper = mount(MessageRow, { props: { message } });
    assertNoForeignElements(wrapper.element);
    expect(wrapper.element.textContent).toContain(payload);
  });
});

describe('08 XSS: название канала (ChannelView)', () => {
  beforeEach(() => {
    store.state.sync.channelsById = {};
  });

  it.each(XSS_PAYLOADS)('payload %s в заголовке канала — просто текст', (payload) => {
    store.state.sync.channelsById[42] = {
      id: 42,
      ext_id: 'c',
      kind: 'dm',
      title: payload,
      tier: null,
      muted: false,
      archived: false,
      map_reason: null,
      last_msg_ts: null,
      my_msg_count: 0,
      first_seen_ts: null,
      unread: 0,
      project_id: null,
    };
    const wrapper = mount(ChannelView, { props: { channelId: 42 } });
    assertNoForeignElements(wrapper.element);
    expect(wrapper.find('h2').element.textContent).toBe(payload);
  });
});

// Ф10-6: левой колонки проектов (`ProjectList`) больше нет — название проекта теперь на вкладке верхней полосы.
describe('08 XSS: название проекта (вкладка TopBar)', () => {
  it.each(XSS_PAYLOADS)('payload %s в названии проекта — просто текст', (payload) => {
    store.state.projects = [
      { id: 1, name: payload, color: null, sort: 0, muted: false, workdir: null, unread: 0 },
    ];
    const wrapper = mount(TopBar);
    assertNoForeignElements(wrapper.element);
    expect(wrapper.get('.project-tab[data-project-id="1"]').element.textContent).toContain(payload);
    wrapper.unmount();
  });
});

describe('08 XSS: причина ошибки outbox (OutboxStatusBadge)', () => {
  it.each(XSS_PAYLOADS)('payload %s в error — просто текст', async (payload) => {
    vi.spyOn(await import('../src/api/client'), 'getOutbox').mockResolvedValue({
      id: 1,
      channel_id: 1,
      project_id: 1,
      text: 'x',
      status: 'failed',
      origin: 'ui',
      error: payload,
      attempts: 1,
      created_ts: 0,
      sent_ts: null,
      ext_id: null,
    } satisfies OutboxItem);

    const wrapper = mount(OutboxStatusBadge, { props: { outboxId: 1, status: 'failed', text: 'x' } });
    await new Promise((resolve) => setTimeout(resolve, 0)); // ждём watch(immediate) -> await api.getOutbox
    await wrapper.vm.$nextTick();
    assertNoForeignElements(wrapper.element);
    expect(wrapper.element.textContent).toContain(payload);
  });
});

describe('08 XSS: панель очереди outbox (F8) — channel_title и text недоверенные', () => {
  it.each(XSS_PAYLOADS)('payload %s в channel_title и text — просто текст', async (payload) => {
    vi.spyOn(await import('../src/api/client'), 'listOutbox').mockResolvedValue([
      {
        id: 1,
        channel_id: 1,
        project_id: 1,
        channel_title: payload,
        text: payload,
        status: 'pending_approval',
        origin: 'api',
        error: null,
        attempts: 0,
        created_ts: 0,
        sent_ts: null,
        ext_id: null,
      } satisfies OutboxListItem,
    ]);

    const wrapper = mount(OutboxQueuePanel, { props: { status: 'pending_approval' } });
    await new Promise((resolve) => setTimeout(resolve, 0)); // ждём onMounted -> await api.listOutbox
    await wrapper.vm.$nextTick();
    assertNoForeignElements(wrapper.element);
    expect(wrapper.element.textContent).toContain(payload);
  });
});
