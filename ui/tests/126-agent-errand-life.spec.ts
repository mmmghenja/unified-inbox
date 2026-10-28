// Ф65-1 (`docs/architecture.md`): жизнь поручения агента в интерфейсе.
// И1 ответ и кнопки видны всегда, «Остановить агента»; И2 блок возврата (reason/by/detail, «Пусть решает»);
// И3 вопрос агента в карточке; И4 итоги агента; И5 «Мой агент берёт сам» в «Автопилоте».
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { AutopilotState, HumanRequest, Project, OwnerItem, OwnerQueue } from '../src/api/types';
import AutopilotPanel from '../src/components/AutopilotPanel.vue';
import HumanCard from '../src/components/HumanCard.vue';
import OwnerCard from '../src/components/OwnerCard.vue';
import OwnerResults from '../src/components/OwnerResults.vue';
import { autopilotUi, resetAutopilot } from '../src/store/autopilot';
import { createHumanState } from '../src/store/humanRequests';
import { store } from '../src/store/instance';
import { createOwnerState } from '../src/store/owner_channel';
import { returnedLine } from '../src/util/owner_channel';

const FIXTURE = JSON.parse(readFileSync(resolve(__dirname, 'fixtures/owner-queue.json'), 'utf8')) as OwnerQueue;
const NOW = 1760360000000;
const MIN = 60000;

function item(ref: string): OwnerItem {
  const found = (JSON.parse(JSON.stringify(FIXTURE)) as OwnerQueue).items.find((i) => i.ref === ref);
  if (found === undefined) throw new Error(`нет карточки ${ref}`);
  return found;
}

function human(over: Partial<HumanRequest> = {}): HumanRequest {
  return {
    id: 7, project_id: 5, kind: 'code', site: 'google.com', url: null, doing: 'регистрация', need: 'Введи код',
    profile: null, tab: null, shot: null, expires_ts: null, status: 'pending', decided_ts: null, decided_by: null,
    errand: null, errand_returned: null, role: 'менеджер', role_no: 1, created_ts: Date.now(), seq: 1, ...over,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}
let sent: Sent[] = [];
let handler: (s: Sent) => Response | undefined;
const posts = (suffix: string) => sent.filter((s) => s.method === 'POST' && decodeURIComponent(s.path).endsWith(suffix));

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.owner_channel = createOwnerState();
  store.state.humans = createHumanState();
  store.state.projects = [{ id: 5, name: 'Hooli', color: '#0a0', sort: 0, muted: false, workdir: '/w', unread: 0 } as Project];
  resetAutopilot();
  sent = [];
  handler = () => undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      sent.push(s);
      const custom = handler(s);
      if (custom !== undefined) return custom;
      if (s.path === '/api/v1/owner_channel/queue') return json({ items: [], seq: 1, agent: { online: true, seen_ts: NOW } });
      if (s.path === '/api/v1/human-requests') return json([]);
      if (s.path === '/api/v1/projects') return json([]);
      return json({ ok: true });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const ERRAND = { id: 77, action: 'solve', label: 'Реши', text: null, created_ts: NOW - MIN, progress: 'проверяю', progress_ts: NOW - 10_000, auto: true };

describe('И1 ответ виден всегда', () => {
  it('поручение открыто: кнопки и поле текста на месте, «Остановить агента» -> DELETE', async () => {
    const card = item('req:104');
    card.errand = ERRAND;
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(w.get('[data-test="owner-errand"]').text()).toContain('решает агент:');
    expect(w.findAll('.owner-actions [data-action]').length).toBeGreaterThan(0);
    expect(w.get('[data-test="owner-take-back"]').text()).toBe('Остановить агента');
    await w.get('[data-test="owner-take-back"]').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'DELETE' && s.path === '/api/v1/owner_channel/errands/77')).toBe(true);
    w.unmount();
  });

  it('текстовое поле ответа не прячется при открытом поручении; ответ владельца уходит (409 errand_open нет)', async () => {
    const card = item('human:7');
    card.errand = ERRAND;
    const text = card.answers.find((a) => a.needs_text);
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    if (text !== undefined) {
      await w.get(`[data-action="${text.action}"]`).trigger('click');
      expect(w.find('textarea.owner-text-input').exists()).toBe(true);
    }
    const first = card.answers.find((a) => !a.needs_text && a.needs_answers !== true)!;
    await w.get(`[data-action="${first.action}"]`).trigger('click');
    await flushPromises();
    expect(posts('/answer')).toHaveLength(1);
    expect(store.state.owner_channel.notice).toBeNull();
    w.unmount();
  });

  it('заявка «нужен человек»: «Готово / Не вышло / Не сейчас» видны при открытом поручении', () => {
    const w = mount(HumanCard, { props: { human: human({ errand: ERRAND }) } });
    expect(w.findAll('.human-actions button').map((b) => b.text())).toEqual(['Готово', 'Не вышло', 'Не сейчас']);
    expect(w.get('[data-test="human-take-back"]').text()).toBe('Остановить агента');
  });
});

describe('И2 блок возврата', () => {
  it('подписи по by: agent / system / ui', () => {
    const ts = new Date(2026, 9, 15, 9, 5).getTime();
    expect(returnedLine({ reason: 'нет доступа', ts, by: 'agent' })).toBe('Агент вернул тебе: нет доступа · 09:05');
    expect(returnedLine({ reason: 'завис: последний шаг «Bash» 6 мин назад', ts, by: 'system' })).toBe(
      'Агент завис: последний шаг «Bash» 6 мин назад · 09:05',
    );
    expect(returnedLine({ reason: 'остановил владелец', ts, by: 'ui' })).toBe('остановил владелец · 09:05');
  });

  it('detail свёрнут до трёх строк и разворачивается; «Пусть решает» с текстом -> POST /errand {text}', async () => {
    const card = item('req:101');
    card.errand = null;
    card.errand_returned = {
      reason: 'не знаю пароль',
      ts: NOW,
      by: 'agent',
      detail: 'строка 1\nстрока 2\nстрока 3\nстрока 4\nстрока 5',
    };
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(w.get('[data-test="owner-returned"]').text()).toContain('Агент вернул тебе: не знаю пароль');
    const detail = w.get('[data-test="owner-returned-detail"]');
    expect(detail.classes()).toContain('errand-returned-detail--clamped');
    await w.get('[data-test="owner-returned-more"]').trigger('click');
    expect(detail.classes()).not.toContain('errand-returned-detail--clamped');
    expect(detail.text()).toContain('строка 5');
    // кнопки владельца остаются
    expect(w.find('[data-action="allow"]').exists()).toBe(true);
    await w.get('[data-test="owner-say"]').setValue('пароль в менеджере паролей');
    await w.get('[data-test="owner-let-decide"]').trigger('click');
    await flushPromises();
    expect(posts('/errand')).toHaveLength(1);
    expect(decodeURIComponent(posts('/errand')[0]!.path)).toBe('/api/v1/owner_channel/req:101/errand');
    expect(posts('/errand')[0]!.body).toEqual({ text: 'пароль в менеджере паролей' });
    w.unmount();
  });

  it('«Пусть решает» без текста — обычное «Реши»; ошибка ядра — под карточкой', async () => {
    const card = item('req:101');
    card.errand = null;
    card.errand_returned = { reason: 'завис: ни одного шага 5 мин', ts: NOW, by: 'system', detail: null };
    handler = (s) => (s.method === 'POST' && s.path.endsWith('/errand') ? json({ detail: 'агент не на связи' }, 409) : undefined);
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(w.get('[data-test="owner-returned"]').text()).toContain('Агент завис: ни одного шага 5 мин');
    expect(w.find('[data-test="owner-returned-detail"]').exists()).toBe(false);
    await w.get('[data-test="owner-let-decide"]').trigger('click');
    await flushPromises();
    expect(posts('/errand')[0]!.body).toEqual({ action: 'solve' });
    expect(w.get('[role="alert"]').text()).toContain('агент не на связи');
    w.unmount();
  });

  it('заявка «нужен человек»: тот же блок, поручение уходит на human:<id>', async () => {
    const w = mount(HumanCard, { props: { human: human({ errand_returned: { reason: 'нет письма', ts: NOW, by: 'agent', detail: 'ждал 10 минут' } }) } });
    expect(w.get('[data-test="human-returned"]').text()).toContain('Агент вернул тебе: нет письма');
    expect(w.get('[data-test="human-returned-detail"]').text()).toBe('ждал 10 минут');
    await w.get('[data-test="human-say"]').setValue('попробуй ещё раз');
    await w.get('[data-test="human-let-decide"]').trigger('click');
    await flushPromises();
    expect(decodeURIComponent(posts('/errand')[0]!.path)).toBe('/api/v1/owner_channel/human:7/errand');
    expect(posts('/errand')[0]!.body).toEqual({ text: 'попробуй ещё раз' });
  });
});

describe('И3 вопрос агента в карточке', () => {
  it('текст вопроса и поле ответа; ответ -> POST /owner_channel/talk {text, reply_to}', async () => {
    const card = item('req:101');
    card.agent_ask = { id: 412, text: 'Какой пароль взять?', ts: NOW, until_ts: NOW + 30 * MIN };
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    expect(w.get('[data-test="owner-ask"]').text()).toContain('Какой пароль взять?');
    expect(w.get('[data-test="owner-ask-send"]').attributes('disabled')).toBeDefined();
    await w.get('[data-test="owner-ask-input"]').setValue('второй');
    await w.get('[data-test="owner-ask-send"]').trigger('click');
    await flushPromises();
    expect(posts('/owner_channel/talk')).toHaveLength(1);
    expect(posts('/owner_channel/talk')[0]!.body).toEqual({ text: 'второй', reply_to: 412 });
    w.unmount();
  });

  it('без вопроса блока нет', () => {
    const w = mount(OwnerCard, { props: { item: item('req:101'), nowMs: NOW, agentOnline: true } });
    expect(w.find('[data-test="owner-ask"]').exists()).toBe(false);
  });
});

describe('И4 итоги агента', () => {
  it('раскрытие читает /owner_channel/closed и показывает только строки с итогом', async () => {
    handler = (s) =>
      s.path === '/api/v1/owner_channel/closed'
        ? json({
            items: [
              { id: 1, ref: 'req:1', kind: 'permission', project_id: 5, closed_ts: NOW, closed_by: 'agent', errand_result: 'разрешил чтение' },
              { id: 2, ref: 'req:2', kind: 'draft', project_id: 5, closed_ts: NOW, closed_by: 'ui', errand_result: null },
              { id: 3, ref: 'req:3', kind: 'press', project_id: 5, closed_ts: NOW, closed_by: 'system', payload: { by: 'auto' }, errand_result: null },
            ],
          })
        : undefined;
    const w = mount(OwnerResults, { attachTo: document.body });
    await flushPromises(); // Ф92-1: итоги читаются при монтировании
    expect(sent.some((s) => s.path === '/api/v1/owner_channel/closed')).toBe(true);
    const details = w.get('details').element as HTMLDetailsElement;
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    await flushPromises();
    const rows = w.findAll('[data-test="owner-result"]');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.text()).toContain('разрешил чтение');
    expect(rows[0]!.text()).toContain('решил: агент');
    expect(rows[1]!.text()).toContain('решил: сразу да');
    w.unmount();
  });
});

describe('Ф66-1 таблица «Кто отвечает на карточки»', () => {
  function state(over: Partial<AutopilotState> = {}): AutopilotState {
    return { switches: [], global: {}, projects: {}, local: true, ...over } as AutopilotState;
  }
  const PROJ = (route?: Record<string, string>) =>
    [{ id: 5, name: 'Hooli', color: '#0a0', sort: 0, muted: false, workdir: '/w', unread: 0, card_route: route }] as unknown as Project[];

  it('строка на группу; «Сразу да» только у групп, где ядро его допускает; ручные сессии — тебе', async () => {
    store.state.projects = PROJ({ draft: 'auto', question: 'agent' });
    autopilotUi.data = state();
    autopilotUi.loaded = true;
    handler = (s) => (s.path === '/api/v1/autopilot' ? json(state()) : undefined);
    const w = mount(AutopilotPanel, { attachTo: document.body });
    await flushPromises();
    const table = w.get('[data-test="route-table"]');
    expect(table.findAll('[data-route-group]')).toHaveLength(12);
    // Ф93-1: строка «Деньги» — только «Мне» / «Агенту», по умолчанию «Мне».
    expect(table.find('[data-route-group="money"] [data-route="auto"]').exists()).toBe(false);
    expect(table.get('[data-route-group="money"] [data-route="me"]').attributes('aria-pressed')).toBe('true');
    expect(table.text()).toContain('Ручные сессии — всегда тебе');
    expect(table.find('[data-route-group="question"] [data-route="auto"]').exists()).toBe(false);
    expect(table.find('[data-route-group="draft"] [data-route="auto"]').exists()).toBe(true);
    expect(table.get('[data-route-group="draft"] [data-route="auto"]').attributes('aria-pressed')).toBe('true');
    expect(table.get('[data-route-group="question"] [data-route="agent"]').attributes('aria-pressed')).toBe('true');
    expect(table.get('[data-route-group="permission"] [data-route="me"]').attributes('aria-pressed')).toBe('true');
    w.unmount();
  });

  it('клик -> PATCH /projects/5 {card_route:{группа:значение}}', async () => {
    store.state.projects = PROJ();
    autopilotUi.data = state();
    autopilotUi.loaded = true;
    handler = (s) => (s.path === '/api/v1/autopilot' ? json(state()) : undefined);
    const w = mount(AutopilotPanel, { attachTo: document.body });
    await flushPromises();
    await w.get('[data-route-group="permission"] [data-route="auto"]').trigger('click');
    await flushPromises();
    const patch = sent.find((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/5');
    expect(patch?.body).toEqual({ card_route: { permission: 'auto' } });
    w.unmount();
  });

  it('с телефона (local: false) таблица работает, выключатели ниже — тоже', async () => {
    store.state.projects = PROJ();
    autopilotUi.data = state({ local: false, switches: [{ key: 'push', group: 'g', title: 'Push', manual: 'ask' }] });
    autopilotUi.loaded = true;
    handler = (s) => (s.path === '/api/v1/autopilot' ? json(autopilotUi.data) : undefined);
    const w = mount(AutopilotPanel, { attachTo: document.body });
    await flushPromises();
    expect(w.get('[data-route-group="permission"] [data-route="agent"]').attributes('disabled')).toBeUndefined();
    await w.get('[data-route-group="permission"] [data-route="agent"]').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'PATCH' && s.path === '/api/v1/projects/5')).toBe(true);
    expect(w.get('.autopilot-global input').attributes('disabled')).toBeUndefined();
    w.unmount();
  });

  it('делегата, «Головной закрывает проект» и «Мой агент берёт сам» в панели больше нет', async () => {
    store.state.projects = PROJ();
    autopilotUi.data = state();
    autopilotUi.loaded = true;
    handler = (s) => (s.path === '/api/v1/autopilot' ? json(state()) : undefined);
    const w = mount(AutopilotPanel, { attachTo: document.body });
    await flushPromises();
    expect(w.text()).not.toContain('Головная закрывает');
    expect(w.text()).not.toContain('Мой агент берёт сам');
    expect(w.text()).not.toContain('подтверждает за меня');
    w.unmount();
  });
});

// Слово владельца: к ответу по карточке сессии можно приложить файлы (только реплика сессии).
describe('файлы к ответу по карточке', () => {
  it('turn/reply: кнопка «Файл» есть, ответ несёт files, без текста тоже; permission/draft — кнопки нет', async () => {
    const card = item('turn:11:4');
    store.state.sessions.files[11] = [{ localId: 1, name: 'a.png', size: 3, kind: 'image', status: 'ready', fileId: 'f1', error: null }];
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    await w.get('[data-action="reply"]').trigger('click');
    expect(w.find('[data-test="owner-attach"]').exists()).toBe(true);
    expect(w.find('.session-files').exists()).toBe(true);
    await w.get('[data-test="owner-text-send"]').trigger('click');
    await flushPromises();
    expect(posts('/answer')[0]!.body).toEqual({ action: 'reply', files: ['f1'] });
    expect(store.state.sessions.files[11]).toEqual([]);
    w.unmount();
    for (const ref of ['req:104', 'draft:55']) {
      const other = mount(OwnerCard, { props: { item: item(ref), nowMs: NOW, agentOnline: true }, attachTo: document.body });
      expect(other.find('[data-test="owner-attach"]').exists()).toBe(false);
      other.unmount();
    }
  });

  it('ошибка отправки: чипы остаются', async () => {
    const card = item('stall:11:1');
    store.state.sessions.files[11] = [{ localId: 2, name: 'b.png', size: 3, kind: 'image', status: 'ready', fileId: 'f2', error: null }];
    handler = (s) => (s.path.endsWith('/answer') ? json({ detail: 'нет' }, 500) : undefined);
    const w = mount(OwnerCard, { props: { item: card, nowMs: NOW, agentOnline: true }, attachTo: document.body });
    await w.get('[data-action="say"]').trigger('click');
    await w.get('[data-test="owner-text-send"]').trigger('click');
    await flushPromises();
    expect(store.state.sessions.files[11]).toHaveLength(1);
    w.unmount();
    store.state.sessions.files[11] = [];
  });
});
