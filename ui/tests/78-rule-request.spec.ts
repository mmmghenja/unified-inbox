// Ф38-2 (`docs/architecture.md`): заявки сессий на правило раскладки — карточка
// «Завести правило?» (кнопки зовут approve/decline, 'stale', 409 с текстом, строки «переложит», не-HTML),
// применение `/sync.rule_requests` и кадра SSE `rule_request`, секция в столбце проекта и в «Ждёт», счётчик.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Project, RuleRequest } from '../src/api/types';
import HomeView from '../src/components/HomeView.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import RuleRequestCard from '../src/components/RuleRequestCard.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { createRuleRequestState } from '../src/store/ruleRequests';
import { createStaleness } from '../src/store/staleness';
import { connectStream, type EventSourceLike } from '../src/store/stream';
import { createStore, HOME_POLL_MS } from '../src/store/store';
import { buildBoard } from '../src/util/board';
import { ruleHeadline, ruleMovedLine, visibleRuleRequests } from '../src/util/ruleRequests';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 9, 5, 15, 0, 0).getTime();
const DAY = 24 * 60 * 60 * 1000;
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function req(overrides: Partial<RuleRequest> = {}): RuleRequest {
  return {
    id: 1,
    seq: 10,
    status: 'pending',
    project_id: 3,
    account_id: 5,
    account_source: 'mail',
    account_title: 'e@umbrella.example.com',
    kind: 'domain',
    value: 'partner.ru',
    description: 'письма от partner.ru',
    reason: 'это партнёры',
    who: 'маркетолог',
    created_ts: NOW - 60_000,
    decided_ts: null,
    rule_id: null,
    moved: 3,
    examples: ['Счёт', 'Акт', 'Договор'],
    ...overrides,
  };
}

function project(overrides: Partial<Project> = {}): Project {
  return { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0, ...overrides };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
  body: unknown;
}

function fakeFetch(handler: (s: Sent) => Response | undefined = () => undefined): {
  sent: Sent[];
  fetchMock: ReturnType<typeof vi.fn>;
} {
  const sent: Sent[] = [];
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input, 'http://localhost');
    const s: Sent = {
      method: init?.method ?? 'GET',
      path: url.pathname,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    sent.push(s);
    const custom = handler(s);
    if (custom !== undefined) return custom;
    if (s.path === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
    if (s.path === '/api/v1/status') return jsonResponse({ accounts: [] });
    if (s.path === '/api/v1/projects' && s.method === 'GET') return jsonResponse([project()]);
    if (s.method === 'GET') return jsonResponse([]);
    return jsonResponse({ ok: true });
  });
  return { sent, fetchMock };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  store.state.ruleRequests = createRuleRequestState();
  store.state.projects = [project()];
  store.state.homeOpen = true;
  store.state.currentProjectId = null;
  store.state.currentChannelId = null;
  delete (window as unknown as { __xss?: number }).__xss;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

describe('Ф38-2: util/ruleRequests', () => {
  it('строка «переложит»: N с примерами, без примеров, 0, null', () => {
    expect(ruleMovedLine(req())).toBe('переложит 3 беседы: Счёт, Акт, Договор');
    expect(ruleMovedLine(req({ moved: 1, examples: ['Счёт'] }))).toBe('переложит 1 беседу: Счёт');
    expect(ruleMovedLine(req({ moved: 12, examples: null }))).toBe('переложит 12 бесед');
    expect(ruleMovedLine(req({ moved: 5, examples: [] }))).toBe('переложит 5 бесед');
    expect(ruleMovedLine(req({ moved: 0 }))).toBe('сейчас не переложит ни одной беседы, будет действовать на новые');
    expect(ruleMovedLine(req({ moved: null }))).toBeNull();
  });

  it('заголовок: источник · аккаунт: условие → проект; названия аккаунта нет — «аккаунт N»', () => {
    expect(ruleHeadline(req(), 'Umbrella')).toBe('Почта · e@umbrella.example.com: письма от partner.ru → Umbrella');
    expect(ruleHeadline(req({ account_title: null, account_source: 'telegram' }), 'Initech')).toBe(
      'Telegram · аккаунт 5: письма от partner.ru → Initech',
    );
    expect(ruleHeadline(req({ account_title: 'Globex', account_source: 'slack' }), 'M')).toBe(
      'Slack · Globex: письма от partner.ru → M',
    );
  });

  it('видны: ждущие всегда; решённые — сутки после решения', () => {
    const list = [
      req({ id: 1 }),
      req({ id: 2, status: 'approved', decided_ts: NOW - DAY + 1000 }),
      req({ id: 3, status: 'declined', decided_ts: NOW - DAY - 1000 }),
      req({ id: 4, status: 'declined', decided_ts: NOW - 1000 }),
      req({ id: 5, status: 'approved', decided_ts: null }),
    ];
    expect(visibleRuleRequests(list, NOW).map((r) => r.id)).toEqual([1, 2, 4]);
  });
});

describe('Ф38-2: карточка', () => {
  it('показывает заголовок, строку «переложит», от кого и причину; «Завести» и «Отклонить» зовут approve / decline', async () => {
    const { sent, fetchMock } = fakeFetch();
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(RuleRequestCard, { props: { request: req() }, attachTo: document.body });
    const text = wrapper.text();
    expect(text).toContain('Правило раскладки');
    expect(text).toContain('Почта · e@umbrella.example.com: письма от partner.ru → Umbrella');
    expect(text).toContain('переложит 3 беседы: Счёт, Акт, Договор');
    expect(text).toContain('от «маркетолог»');
    expect(text).toContain('Причина: это партнёры');
    await wrapper.get('button.rule-request-approve').trigger('click');
    await flushPromises();
    await wrapper.get('button.rule-request-decline').trigger('click');
    await flushPromises();
    const posts = sent.filter((s) => s.method === 'POST').map((s) => s.path);
    expect(posts).toContain('/api/v1/rule-requests/1/approve');
    expect(posts).toContain('/api/v1/rule-requests/1/decline');
    wrapper.unmount();
  });

  it('кнопки disabled на время запроса', async () => {
    let release: (r: Response) => void = () => {};
    const { fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/rule-requests/1/approve'
        ? (new Promise<Response>((resolve) => {
            release = resolve;
          }) as unknown as Response)
        : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(RuleRequestCard, { props: { request: req() } });
    await wrapper.get('button.rule-request-approve').trigger('click');
    expect(wrapper.get('button.rule-request-approve').attributes('disabled')).toBeDefined();
    expect(wrapper.get('button.rule-request-decline').attributes('disabled')).toBeDefined();
    release(jsonResponse(req({ status: 'approved' })));
    await flushPromises();
    expect(wrapper.get('button.rule-request-approve').attributes('disabled')).toBeUndefined();
  });

  it('решённая: без кнопок; «заведено, переложено N» (фактическое moved) / «отклонено»', () => {
    const approved = mount(RuleRequestCard, {
      props: { request: req({ status: 'approved', decided_ts: NOW, moved: 2, examples: null }) },
    });
    expect(approved.text()).toContain('заведено, переложено 2');
    expect(approved.text()).not.toContain('переложит');
    expect(approved.find('button').exists()).toBe(false);
    const declined = mount(RuleRequestCard, { props: { request: req({ status: 'declined', decided_ts: NOW }) } });
    expect(declined.text()).toContain('отклонено');
    expect(declined.find('button').exists()).toBe(false);
  });

  it('409 «заявка уже решена» — заявка перечитана, строка «уже изменилась»', async () => {
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/rule-requests/1/approve' ? jsonResponse({ detail: 'заявка уже решена (approved)' }, 409) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(RuleRequestCard, { props: { request: req() } });
    await wrapper.get('button.rule-request-approve').trigger('click');
    await flushPromises();
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/rule-requests')).toBe(true);
    expect(wrapper.get('.rule-request-error').text()).toContain('уже изменилась');
  });

  it('409 «правило не завести: …» — текст ядра под кнопками, кнопки остались', async () => {
    const { fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/rule-requests/1/approve'
        ? jsonResponse({ detail: 'правило не завести: папки больше нет' }, 409)
        : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(RuleRequestCard, { props: { request: req() } });
    await wrapper.get('button.rule-request-approve').trigger('click');
    await flushPromises();
    expect(wrapper.get('.rule-request-error').text()).toBe('правило не завести: папки больше нет');
    expect(wrapper.find('button.rule-request-approve').exists()).toBe(true);
  });

  it('ошибка сети — «Не отправилось…»', async () => {
    const { fetchMock } = fakeFetch((s) => {
      if (s.path === '/api/v1/rule-requests/1/approve') throw new TypeError('network down');
      return undefined;
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const wrapper = mount(RuleRequestCard, { props: { request: req() } });
    await wrapper.get('button.rule-request-approve').trigger('click');
    await flushPromises();
    expect(wrapper.get('.rule-request-error').text()).toContain('Не отправилось');
  });

  it('описание, причина, имя сессии и названия бесед — только текстом, не HTML', () => {
    const wrapper = mount(RuleRequestCard, {
      props: {
        request: req({
          description: XSS,
          reason: XSS,
          who: XSS,
          account_title: XSS,
          examples: [XSS, XSS],
          moved: 2,
        }),
      },
      attachTo: document.body,
    });
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.find('script').exists()).toBe(false);
    expect(wrapper.text()).toContain('<img src=x');
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
    wrapper.unmount();
  });
});

describe('Ф38-2: стор — /sync и кадр SSE', () => {
  it('/sync.rule_requests попадает в стор; старшая по seq побеждает; решённая перестаёт ждать', async () => {
    let calls = 0;
    const { fetchMock } = fakeFetch((s) => {
      if (s.path !== '/api/v1/sync') return undefined;
      calls += 1;
      if (calls === 1) {
        return jsonResponse({ messages: [], channels: [], max_seq: 11, more: false, rule_requests: [req({ seq: 11 })] });
      }
      if (calls === 2) {
        return jsonResponse({
          messages: [],
          channels: [],
          max_seq: 12,
          more: false,
          rule_requests: [req({ seq: 9, status: 'declined' }), req({ id: 2, seq: 12 })],
        });
      }
      return jsonResponse({
        messages: [],
        channels: [],
        max_seq: 13,
        more: false,
        rule_requests: [req({ seq: 13, status: 'approved', decided_ts: NOW, moved: 4 })],
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.runSync();
    expect(s.state.ruleRequests.items[1]!.status).toBe('pending');
    await s.runSync();
    expect(s.state.ruleRequests.items[1]!.status).toBe('pending'); // seq 9 старше 11 — не откатывает
    expect(s.state.ruleRequests.items[2]).toBeDefined();
    await s.runSync();
    expect(s.state.ruleRequests.items[1]!.status).toBe('approved');
    expect(s.state.ruleRequests.items[1]!.moved).toBe(4);
  });

  it('кадр SSE rule_request запускает /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      readyState: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      close: () => {},
    };
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('rule_request')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
  });

  it('loadRuleRequests читает GET /rule-requests', async () => {
    const { sent, fetchMock } = fakeFetch((s) => (s.path === '/api/v1/rule-requests' ? jsonResponse([req()]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    const s = createStore();
    await s.loadRuleRequests();
    expect(sent.some((x) => x.path === '/api/v1/rule-requests')).toBe(true);
    expect(s.state.ruleRequests.items[1]!.id).toBe(1);
  });
});

describe('Ф38-2: главная и «Ждёт»', () => {
  const HOME = { react: [], drafts: [], runs: [], sent: [] };
  const WORK = { tasks: {}, pending: {}, stale: {} };

  it('доска: заявка — в столбце проекта-цели, ждущая входит в счётчик, решённая видна, но не считается', () => {
    const board = buildBoard(
      [project(), project({ id: 4, name: 'Личное' })],
      {
        ...HOME,
        ruleRequests: [
          req({ id: 1 }),
          req({ id: 2, status: 'approved', decided_ts: NOW - 1000 }),
          req({ id: 3, status: 'declined', decided_ts: NOW - DAY - 1000 }),
          req({ id: 4, project_id: 4 }),
        ],
      },
      NOW,
    );
    expect(board.columns[0]!.ruleRequests.map((r) => r.id)).toEqual([1, 2]);
    expect(board.columns[0]!.waitCount).toBe(1);
    expect(board.columns[1]!.ruleRequests.map((r) => r.id)).toEqual([4]);
  });

  it('секция rule-requests в «Нужна реакция» столбца проекта', () => {
    const board = buildBoard([project()], { ...HOME, ruleRequests: [req()] }, NOW);
    const wrapper = mount(ProjectColumn, { props: { column: board.columns[0]! } });
    expect(wrapper.get('[data-section="react"] .board-section-title').text()).toBe('Нужна реакция · 1');
    expect(wrapper.find('[data-section="react"] [data-section="rule-requests"] .rule-request-card').exists()).toBe(true);
    expect(wrapper.find('button.rule-request-approve').exists()).toBe(true);
  });

  it('«Ждёт»: только ждущие заявки — решённая в списке не висит', () => {
    const input = {
      projects: [project()],
      home: HOME,
      
      nowMs: NOW,
      ruleRequests: [req({ id: 1 }), req({ id: 2, status: 'approved', decided_ts: NOW - 1000 })],
    };
    expect(buildWaiting(input).filter((i) => i.kind === 'rule').map((i) => i.key)).toEqual(['rule-1']);
    expect(homeDecisionCount(input)).toBe(1);
  });

  it('WaitList рисует карточку заявки', async () => {
    const { fetchMock } = fakeFetch((s) => (s.path === '/api/v1/rule-requests' ? jsonResponse([req()]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    store.state.ruleRequests.items[1] = req();
    const wrapper = mount(WaitList);
    await flushPromises();
    expect(wrapper.find('.wait-item--rule .rule-request-card').exists()).toBe(true);
    wrapper.unmount();
  });

  // Ф38-3 A3: число «переложит N» ядро считает при каждой выдаче; «Ждёт» и главная перечитывают заявки раз в
  // HOME_POLL_MS, при равном seq берётся свежая строка — число на карточке не старше опроса.
  it('«переложит N» обновляется опросом: тот же seq, свежее число', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let moved = 3;
    const { sent, fetchMock } = fakeFetch((s) =>
      s.path === '/api/v1/rule-requests' ? jsonResponse([req({ moved, examples: [] })]) : undefined,
    );
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(WaitList);
    await flushPromises();
    expect(wrapper.get('.rule-request-card').text()).toContain('переложит 3 ');
    moved = 5;
    vi.advanceTimersByTime(HOME_POLL_MS);
    await flushPromises();
    expect(sent.filter((x) => x.path === '/api/v1/rule-requests')).toHaveLength(2);
    expect(wrapper.get('.rule-request-card').text()).toContain('переложит 5 ');
    wrapper.unmount();
  });

  it('главная за один опрос читает заявки один раз', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const { sent, fetchMock } = fakeFetch((s) => (s.path === '/api/v1/rule-requests' ? jsonResponse([req()]) : undefined));
    vi.stubGlobal('fetch', fetchMock);
    const wrapper = mount(HomeView);
    await flushPromises();
    const count = () => sent.filter((x) => x.path === '/api/v1/rule-requests').length;
    expect(count()).toBe(1);
    vi.advanceTimersByTime(HOME_POLL_MS);
    await flushPromises();
    expect(count()).toBe(2);
    wrapper.unmount();
  });
});
