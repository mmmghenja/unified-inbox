// Ф54-1: заявки «нужен человек» в интерфейсе (`docs/architecture.md`, 12а):
// карточка из `/sync`, три исхода шлют свои ручки, решённая исчезает, порядок и срок, скриншот только при `shot`,
// чужой текст — не HTML, кадр SSE `human_request`, ключи автопилота `scope`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HumanRequest, Project } from '../src/api/types';
import HumanCard from '../src/components/HumanCard.vue';
import WaitList from '../src/components/WaitList.vue';
import { store } from '../src/store/instance';
import { applyHumanRequest, createHumanState } from '../src/store/humanRequests';
import { createStaleness } from '../src/store/staleness';
import { connectStream, type EventSourceLike } from '../src/store/stream';
import { autopilotUi, catalogSize } from '../src/store/autopilot';
import { groupSwitches } from '../src/util/autopilot';
import { buildBoard } from '../src/util/board';
import { humanDeadline, humanDecidedBy, visibleHumans } from '../src/util/humanRequests';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 9, 13, 15, 0, 0).getTime();
const MIN = 60000;
const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>';

function human(overrides: Partial<HumanRequest> = {}): HumanRequest {
  return {
    id: 1,
    project_id: 5,
    kind: 'captcha',
    site: 'jobs.example',
    url: 'https://jobs.example/login?x=1',
    doing: 'подаю заявку',
    need: 'Реши капчу',
    profile: 'work',
    tab: 'Вкладка 2',
    shot: null,
    expires_ts: null,
    status: 'pending',
    decided_ts: null,
    decided_by: null,
    role: 'менеджер 2',
    role_no: 2,
    created_ts: NOW,
    seq: 1,
    ...overrides,
  };
}

function project(): Project {
  return { id: 5, name: 'Hooli', color: '#00aa00', sort: 0, muted: false, workdir: null, unread: 0 } as Project;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

interface Sent {
  method: string;
  path: string;
}
let sent: Sent[] = [];
let handler: (s: Sent) => Response | undefined = () => undefined;

beforeEach(() => {
  store.state.humans = createHumanState();
  store.state.projects = [project()];
  sent = [];
  handler = () => undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname };
      sent.push(s);
      return handler(s) ?? json([]);
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('54-1 стор и util', () => {
  it('редьюсер: старший seq побеждает', () => {
    const state = createHumanState();
    applyHumanRequest(state, human({ seq: 5, status: 'done' }));
    applyHumanRequest(state, human({ seq: 3, status: 'pending' }));
    expect(state.items[1]!.status).toBe('done');
  });

  it('/sync: раздел human_requests попадает в стор', () => {
    (store as unknown as { applySyncBatch(r: unknown): void }).applySyncBatch({ channels: [], messages: [], human_requests: [human({ id: 7 })] });
    expect(store.state.humans.items[7]!.need).toBe('Реши капчу');
  });

  it('срок: «осталось N мин» / «без срока»; источник ответа — слова', () => {
    expect(humanDeadline(human({ expires_ts: NOW + 12 * MIN }), NOW)).toBe('осталось 12 мин');
    expect(humanDeadline(human(), NOW)).toBe('без срока');
    expect(humanDecidedBy(human({ decided_by: 'agent' }))).toBe('агент');
    expect(humanDecidedBy(human({ decided_by: 'ui' }))).toBe('Владелец');
    expect(humanDecidedBy(human({ decided_by: null }))).toBeNull();
  });

  it('видимые — только ждущие; срок меньше 10 минут — первым', () => {
    const list = visibleHumans(
      [human({ id: 1 }), human({ id: 2, expires_ts: NOW + 5 * MIN }), human({ id: 3, status: 'done' }), human({ id: 4, expires_ts: NOW + 30 * MIN })],
      NOW,
    );
    expect(list.map((h) => h.id)).toEqual([2, 1, 4]);
  });
});

describe('54-1 порядок в «Ждёт»', () => {
  const base = {
    projects: [project()],
    home: { react: [], drafts: [] },
    work: { tasks: {}, pending: {}, stale: {} },
    nowMs: NOW,
  };

  it('заявка выше черновиков, со сроком < 10 минут — самая первая; входит в счётчик главной', () => {
    const draft = {
      id: 9,
      project_id: 5,
      project_name: 'Hooli',
      created_ts: NOW,
    } as unknown as Parameters<typeof buildWaiting>[0]['home']['drafts'][number];
    const items = buildWaiting({
      ...base,
      home: { react: [], drafts: [draft] },
      humans: [human({ id: 1 }), human({ id: 2, expires_ts: NOW + 3 * MIN })],
    });
    expect(items.map((i) => i.key)).toEqual(['human-2', 'human-1', 'draft-9']);
    expect(homeDecisionCount({ ...base, humans: [human({ id: 1 }), human({ id: 3, status: 'later' })] })).toBe(1);
  });

  it('доска: заявка в столбце проекта и в его счётчике', () => {
    const board = buildBoard(
      [project()],
      { react: [], drafts: [], runs: [], sent: [], humans: [human({ id: 1 }), human({ id: 2, status: 'done' })] },
      NOW,
    );
    const column = board.columns.find((c) => c.project.id === 5)!;
    expect(column.humans.map((h) => h.id)).toEqual([1]);
    expect(column.waitCount).toBe(1);
  });

  it('WaitList показывает карточку из стора', () => {
    store.state.humans.items[1] = human();
    const w = mount(WaitList);
    expect(w.find('.human-card').exists()).toBe(true);
  });
});

describe('54-1 карточка', () => {
  it('вид, проект и роль, сайт, нужно, делаю, профиль · вкладка, срок', () => {
    const w = mount(HumanCard, { props: { human: human({ kind: 'login', expires_ts: Date.now() + 20 * MIN }) } });
    const t = w.text();
    expect(t).toContain('Вход');
    expect(t).toContain('Hooli · менеджер 2');
    expect(t).toContain('jobs.example');
    expect(t).toContain('Реши капчу');
    expect(t).toContain('подаю заявку');
    expect(t).toContain('work · Вкладка 2');
    expect(t).toMatch(/осталось \d+ мин/);
    expect(mount(HumanCard, { props: { human: human() } }).text()).toContain('без срока');
  });

  it('url — простым текстом, не ссылкой; «копировать» кладёт его в буфер', async () => {
    const copied: string[] = [];
    vi.stubGlobal('navigator', { clipboard: { writeText: async (s: string) => void copied.push(s) } });
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    const w = mount(HumanCard, { props: { human: human() } });
    expect(w.find('a').exists()).toBe(false);
    expect(w.text()).toContain('https://jobs.example/login?x=1');
    await w.get('.human-copy').trigger('click');
    await flushPromises();
    expect(copied).toEqual(['https://jobs.example/login?x=1']);
  });

  it('скриншот: без shot картинки нет; с shot — миниатюра, по клику окно с полным', async () => {
    const none = mount(HumanCard, { props: { human: human() } });
    expect(none.find('img').exists()).toBe(false);
    const w = mount(HumanCard, { props: { human: human({ id: 4, shot: { size: 10, mime: 'image/png' } }) }, attachTo: document.body });
    expect(w.get('.human-shot img').attributes('src')).toBe('/api/v1/human-requests/4/shot');
    expect(document.querySelector('.human-shot-full')).toBeNull();
    await w.get('.human-shot').trigger('click');
    expect(document.querySelector('.human-shot-full')!.getAttribute('src')).toBe('/api/v1/human-requests/4/shot');
    w.unmount();
  });

  it('три кнопки шлют done / failed / later', async () => {
    handler = (s) => (s.method === 'POST' ? json(human({ seq: 9, status: 'done' })) : undefined);
    const w = mount(HumanCard, { props: { human: human() } });
    for (const [cls, outcome] of [['.human-done', 'done'], ['.human-failed', 'failed'], ['.human-later', 'later']] as const) {
      await w.get(cls).trigger('click');
      await flushPromises();
      expect(sent.some((s) => s.method === 'POST' && s.path === `/api/v1/human-requests/1/${outcome}`)).toBe(true);
    }
  });

  it('решённая исчезает из «Ждёт»', async () => {
    store.state.humans.items[1] = human();
    handler = (s) => (s.method === 'POST' ? json(human({ seq: 9, status: 'done', decided_by: 'ui' })) : undefined);
    const w = mount(WaitList);
    await w.get('.human-done').trigger('click');
    await flushPromises();
    expect(w.find('.human-card').exists()).toBe(false);
  });

  it('409 — текст detail под кнопками, список перечитан', async () => {
    handler = (s) => (s.method === 'POST' ? json({ detail: 'заявка уже закрыта: готово' }, 409) : undefined);
    const w = mount(HumanCard, { props: { human: human() } });
    await w.get('.human-later').trigger('click');
    await flushPromises();
    expect(w.text()).toContain('заявка уже закрыта: готово');
    expect(sent.some((s) => s.method === 'GET' && s.path === '/api/v1/human-requests')).toBe(true);
  });

  it('закрытая заявка: кнопок нет, источник ответа мелко', () => {
    const w = mount(HumanCard, { props: { human: human({ status: 'done', decided_by: 'agent' }) } });
    expect(w.find('.human-actions').exists()).toBe(false);
    expect(w.text()).toContain('ответил: агент');
    expect(mount(HumanCard, { props: { human: human() } }).text()).not.toContain('ответил');
  });

  it('Ф93-2: причина исхода — строкой под закрытой заявкой, текстом', () => {
    const note = '<b>сайт</b> просит СМС на чужой номер';
    const w = mount(HumanCard, { props: { human: human({ status: 'failed', decided_by: 'agent', decided_note: note }) } });
    expect(w.find('.human-note').text()).toBe(`причина: ${note}`);
    expect(w.find('.human-note b').exists()).toBe(false);
    expect(mount(HumanCard, { props: { human: human({ status: 'failed', decided_by: 'agent' }) } }).find('.human-note').exists()).toBe(false);
  });

  it('текст автора с разметкой показан как текст', () => {
    const w = mount(HumanCard, {
      props: { human: human({ site: XSS, url: XSS, doing: XSS, need: XSS, profile: XSS, tab: XSS, role: XSS }) },
    });
    expect(w.find('img').exists()).toBe(false);
    expect(w.find('script').exists()).toBe(false);
    expect(w.find('.human-need').text()).toBe(XSS);
  });
});

describe('54-1 кадр SSE и автопилот', () => {
  it('кадр human_request будит /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      addEventListener: (name: string, fn: () => void) => void listeners.set(name, fn),
      close: () => undefined,
      onerror: null,
      onopen: null,
    } as unknown as EventSourceLike;
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('human_request')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
  });

  it('автопилот: ключ scope=project в группе «Нужен человек»; M сессии — только ключи scope=session', () => {
    const switches = [
      { key: 'hub_press', group: 'Сообщения людям', title: 'нажатие кнопок ботов', manual: 'ask' as const, scope: 'session' as const },
      { key: 'hub_send', group: 'Сообщения людям', title: 'отправка', manual: 'ask' as const },
      { key: 'human_agent', group: 'Нужен человек', title: 'капча', manual: 'ask' as const, scope: 'project' as const, note: 'действует на заявки любых сессий проекта, и ручных' },
    ];
    const groups = groupSwitches(switches);
    expect(groups.map((g) => g.title)).toContain('Нужен человек');
    autopilotUi.data = { switches, global: {}, projects: {}, local: true };
    expect(catalogSize()).toBe(2);
  });
});
