// Ф63-1 (`docs/architecture.md`): блок «Правила входящих» в «Раскладке» и
// пометка «правило #N» у сообщения.
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { InboundRule, Message, Project, StatusResponse } from '../src/api/types';
import MappingPanel from '../src/components/MappingPanel.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { store } from '../src/store/instance';
import { layout } from '../src/util/layout';

function rule(overrides: Partial<InboundRule> = {}): InboundRule {
  return {
    id: 7,
    project_id: 3,
    title: 'Заказы',
    enabled: true,
    match: { accounts: [], from: ['market.example.com'], subject: [], text: [], exclude: [], kinds: [] },
    action: { wake: ['менеджер'], attention: 'react' },
    description: 'письма с market.example.com',
    created_by_role: null,
    created_by_session: null,
    created_ts: 1,
    updated_ts: 1,
    hits: 12,
    last_hit_ts: 1_760_000_000_000,
    wake_status: [{ role: 'менеджер', listening: 'none' }],
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function fakeFetch(opts: { rules?: InboundRule[]; postStatus?: number } = {}) {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: url.pathname,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    sent.push(s);
    const base = '/api/v1/projects/3/inbound-rules';
    if (s.path === base && s.method === 'GET') {
      return jsonResponse({
        project_id: 3,
        rules: opts.rules ?? [rule()],
        limits: { rules: 50, list: 20, title: 80, words: 6 },
      });
    }
    if (s.path === base && s.method === 'POST') {
      if (opts.postStatus !== undefined && opts.postStatus !== 201) {
        return jsonResponse({ detail: 'text 1: больше 6 слов' }, opts.postStatus);
      }
      return jsonResponse(rule({ id: 8, title: String((s.body as { title: string }).title), hits: 0 }), 201);
    }
    if (s.method === 'PATCH') return jsonResponse(rule({ enabled: (s.body as { enabled: boolean }).enabled }));
    if (s.method === 'DELETE') return jsonResponse({ removed: 7, project_id: 3 });
    return jsonResponse([]);
  });
  return { sent, fetchMock };
}

const project: Project = { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0 };

enableAutoUnmount(afterEach);

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  layout.mode = 'full';
  store.state.projects = [project];
  store.state.status = { accounts: [{ id: 5, source: 'mail', title: 'ящик' }] } as unknown as StatusResponse;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

async function openProject(f: ReturnType<typeof fakeFetch>) {
  vi.stubGlobal('fetch', f.fetchMock);
  const wrapper = mount(MappingPanel, { attachTo: document.body });
  await flushPromises();
  await wrapper.get('.inbound-project').setValue('3');
  await flushPromises();
  return wrapper;
}

describe('Ф63-1: «Правила входящих»', () => {
  it('список: название, условие, действие, счётчик, «не слушает»', async () => {
    const wrapper = await openProject(fakeFetch());
    const text = wrapper.get('.inbound-rule').text();
    expect(text).toContain('#7 Заказы');
    expect(text).toContain('письма с market.example.com');
    expect(text).toContain('будит: менеджер');
    expect(text).toContain('внимание: реагирую');
    expect(text).toContain('сработало 12');
    expect(text).toContain('не слушает: менеджер');
  });

  it('добавление шлёт поля по контракту и показывает новую строку', async () => {
    const f = fakeFetch();
    const wrapper = await openProject(f);
    await wrapper.get('.inbound-title').setValue('Новое');
    await wrapper.get('.inbound-from').setValue('@market.example.com, a@b.ru');
    await wrapper.get('.inbound-subject').setValue('новое сообщение по заказу');
    await wrapper.get('.inbound-wake').setValue('менеджер');
    await wrapper.get('.inbound-attention').setValue('info');
    await wrapper.get('.inbound-account').setValue(true);
    await wrapper.get('.inbound-kind').setValue(true);
    await wrapper.get('.inbound-add').trigger('submit');
    await flushPromises();
    const post = f.sent.find((s) => s.method === 'POST');
    expect(post?.body).toEqual({
      title: 'Новое',
      accounts: [5],
      from: ['@market.example.com', 'a@b.ru'],
      subject: ['новое сообщение по заказу'],
      text: [],
      exclude: [],
      kinds: ['dm'],
      wake: ['менеджер'],
      attention: 'info',
      archive: null,
    });
    expect(wrapper.findAll('.inbound-rule')).toHaveLength(2);
  });

  it('галочка «Сразу в архив» шлёт archive: true', async () => {
    const f = fakeFetch();
    const wrapper = await openProject(f);
    await wrapper.get('.inbound-title').setValue('Тестовые заявки');
    await wrapper.get('.inbound-subject').setValue('Order Notification');
    await wrapper.get('.inbound-archive').setValue(true);
    await wrapper.get('.inbound-add').trigger('submit');
    await flushPromises();
    const post = f.sent.find((s) => s.method === 'POST');
    expect(post?.body).toMatchObject({ subject: ['Order Notification'], archive: true, attention: null });
  });

  it('ошибка 422 показывается текстом ответа, строка не добавляется', async () => {
    const wrapper = await openProject(fakeFetch({ postStatus: 422 }));
    await wrapper.get('.inbound-title').setValue('Плохое');
    await wrapper.get('.inbound-add').trigger('submit');
    await flushPromises();
    expect(wrapper.get('.inbound-error').text()).toContain('text 1: больше 6 слов');
    expect(wrapper.findAll('.inbound-rule')).toHaveLength(1);
  });

  it('выключатель шлёт PATCH enabled', async () => {
    const f = fakeFetch();
    const wrapper = await openProject(f);
    await wrapper.get('.inbound-enabled').setValue(false);
    await flushPromises();
    const patch = f.sent.find((s) => s.method === 'PATCH');
    expect(patch?.path).toBe('/api/v1/projects/3/inbound-rules/7');
    expect(patch?.body).toEqual({ enabled: false });
  });

  it('удаление — только после подтверждения', async () => {
    const f = fakeFetch();
    const wrapper = await openProject(f);
    await wrapper.get('.inbound-delete').trigger('click');
    expect(f.sent.some((s) => s.method === 'DELETE')).toBe(false);
    await wrapper.get('.inbound-delete-confirm').trigger('click');
    await flushPromises();
    expect(f.sent.some((s) => s.method === 'DELETE' && s.path.endsWith('/inbound-rules/7'))).toBe(true);
    expect(wrapper.findAll('.inbound-rule')).toHaveLength(0);
  });

  it('телефон: список и выключатель, без формы и «Удалить»', async () => {
    layout.mode = 'phone';
    const wrapper = await openProject(fakeFetch());
    expect(wrapper.find('.inbound-rule').exists()).toBe(true);
    expect(wrapper.find('.inbound-enabled').exists()).toBe(true);
    expect(wrapper.find('.inbound-add').exists()).toBe(false);
    expect(wrapper.find('.inbound-delete').exists()).toBe(false);
  });
});

describe('Ф63-1: пометка у сообщения', () => {
  const base: Message = {
    id: 1, seq: 1, first_seq: 1, channel_id: 1, project_id: 3, ext_id: '1', thread_ext_id: null,
    reply_to_ext_id: null, grouped_id: null, is_me: false, kind: 'text', text: 'привет', entities_json: null,
    attachments_json: null, forward_json: null, mentions_me: false, refs_json: null, reactions_json: null,
    ts: 1000, edited_ts: null, deleted: false, outbox_id: null, outbox_status: null,
    sender: { id: 1, name: 'Коллега', handle: null },
  } as Message;

  it('«правило #N» с названием в подсказке; без rule_hits пометки нет', () => {
    const withRule = mount(MessageRow, { props: { message: { ...base, rule_hits: [7], rules: [{ id: 7, title: 'Заказы' }] } } });
    const mark = withRule.get('.rule-mark');
    expect(mark.text()).toBe('правило #7');
    expect(mark.attributes('title')).toContain('Заказы');
    const without = mount(MessageRow, { props: { message: base } });
    expect(without.find('.rule-mark').exists()).toBe(false);
  });
});
