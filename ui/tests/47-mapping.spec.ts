// Ф14-1 C (`docs/architecture.md`, тесты D 14–17): раскладка из интерфейса.
// 14 — чистые функции `util/mapping.ts`; 15 — выбор проекта в шапке беседы (`PATCH` с `project_id` / `null` /
// `map_source: "auto"`, строка про сессии Claude); 16 — выбор бесед в списке и перенос пачкой; 17 — панель
// «Раскладка»: правила по аккаунтам, «Добавить» только после «Проверить», удаление через предпросмотр, новый проект.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Account, Channel, MapRule, Project, StatusResponse } from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelView from '../src/components/ChannelView.vue';
import MappingPanel from '../src/components/MappingPanel.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';
import { layout } from '../src/util/layout';
import {
  addPreviewLine,
  besed,
  claudeSessionsLine,
  deletePreviewLine,
  exampleLine,
  freeColor,
  mapReasonLabel,
  MAP_AUTO,
  MAP_UNSORTED,
  moveTargetOptions,
  optionCountLabel,
  PROJECT_PALETTE,
  projectSelectOptions,
  projectSelectValue,
  targetOf,
} from '../src/util/mapping';

function project(id: number, name: string, extra: Partial<Project> = {}): Project {
  return { id, name, color: null, sort: id, muted: false, workdir: null, unread: 0, ...extra };
}

const PROJECTS: Project[] = [
  project(1, 'Globex', { workdir: '/w/globex', color: '#e53935' }),
  project(4, 'Личное', { color: '#1e88e5' }),
  project(-1, 'Неразобранное'),
  project(-2, 'Все'),
];

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 1,
    ext_id: 'c1',
    kind: 'dm',
    title: 'Алиса',
    tier: null,
    muted: false,
    archived: false,
    map_reason: 'нет правил',
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: null,
    map_source: 'auto',
    ...overrides,
  };
}

function account(overrides: Partial<Account>): Account {
  return {
    id: 1,
    source: 'telegram',
    title: 'Личный TG',
    status: 'ok',
    last_ok_ts: null,
    last_error: null,
    flood_until: null,
    ...overrides,
  };
}

interface Call {
  method: string;
  path: string;
  search: string;
  body: unknown;
}

type Handler = (call: Call) => Response | undefined;
let handler: Handler = () => undefined;
const calls: Call[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function callsOf(method: string, path: string): Call[] {
  return calls.filter((c) => c.method === method && c.path === `/api/v1${path}`);
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  calls.length = 0;
  handler = () => undefined;
  layout.mode = 'full';
  store.state.sync.channelsById = {};
  store.state.homeOpen = false;
  store.state.currentProjectId = -1;
  store.state.currentChannelId = null;
  store.state.openThread = null;
  store.state.projects = PROJECTS.map((p) => ({ ...p }));
  store.state.status = {
    accounts: [account({ id: 1 }), account({ id: 5, source: 'mail', title: 'Почта ИИП' })],
  } as unknown as StatusResponse;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const call: Call = {
        method: init?.method ?? 'GET',
        path: url.pathname,
        search: url.search,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      calls.push(call);
      const custom = handler(call);
      if (custom !== undefined) return custom;
      if (url.pathname === '/api/v1/sync') return jsonResponse({ messages: [], channels: [], max_seq: 1, more: false });
      if (url.pathname === '/api/v1/projects' && call.method === 'GET') return jsonResponse(PROJECTS);
      if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
      if (url.pathname.endsWith('/channels')) return jsonResponse([]);
      if (url.pathname === '/api/v1/map-rules' && call.method === 'GET') return jsonResponse([]);
      return jsonResponse({ ok: true });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  layout.mode = 'full';
  document.body.innerHTML = '';
});

// --- 14: util/mapping.ts --------------------------------------------------------------------------------------

describe('Ф14-1 D14: util/mapping', () => {
  it('значение выбора: авто -> auto; ручная -> её проект или «Неразобранное»; без map_source — по map_reason', () => {
    expect(projectSelectValue({ map_source: 'auto', map_reason: 'правило #3: x', project_id: 4 })).toBe(MAP_AUTO);
    expect(projectSelectValue({ map_source: 'manual', map_reason: 'manual', project_id: 4 })).toBe('4');
    expect(projectSelectValue({ map_source: 'manual', map_reason: 'manual', project_id: null })).toBe(MAP_UNSORTED);
    expect(projectSelectValue({ map_reason: 'manual', project_id: null })).toBe(MAP_UNSORTED);
    expect(projectSelectValue({ map_reason: 'нет правил', project_id: null })).toBe(MAP_AUTO);
  });

  it('подписи выбора: «По правилам: <проект>» у авто, « · Claude» у проекта с папкой, «Неразобранное» в конце', () => {
    const auto = projectSelectOptions(PROJECTS, { map_source: 'auto', map_reason: 'x', project_id: 1 });
    expect(auto).toEqual([
      { value: 'auto', label: 'По правилам: Globex' },
      { value: '1', label: 'Globex · Claude' },
      { value: '4', label: 'Личное' },
      { value: 'none', label: 'Неразобранное' },
    ]);
    expect(projectSelectOptions(PROJECTS, { map_source: 'auto', map_reason: 'x', project_id: null })[0].label).toBe(
      'По правилам: Неразобранное',
    );
    expect(projectSelectOptions(PROJECTS, { map_source: 'manual', map_reason: 'manual', project_id: 4 })[0].label).toBe(
      'По правилам',
    );
    expect(moveTargetOptions(PROJECTS).map((o) => o.value)).toEqual(['1', '4', 'none', 'auto']);
  });

  it('targetOf: auto / null / номер; мусор — undefined', () => {
    expect(targetOf('auto')).toBe('auto');
    expect(targetOf('none')).toBeNull();
    expect(targetOf('4')).toBe(4);
    expect(targetOf('')).toBeUndefined();
    expect(targetOf('-1')).toBeUndefined();
    expect(targetOf('1.5')).toBeUndefined();
  });

  it('mapReasonLabel', () => {
    expect(mapReasonLabel('manual')).toBe('вручную');
    expect(mapReasonLabel('правило #12: папка «Initech»')).toBe('папка «Initech»');
    expect(mapReasonLabel('нет правил')).toBe('правил нет');
    expect(mapReasonLabel('как у форума')).toBe('как у форума');
    expect(mapReasonLabel('новая беседа')).toBe('новая беседа');
    expect(mapReasonLabel(null)).toBeNull();
  });

  it('строка про сессии Claude, счёт и строки предпросмотра, цвет нового проекта', () => {
    expect(claudeSessionsLine(PROJECTS, 1)).toBe('беседу видят сессии Claude проекта «Globex»');
    expect(claudeSessionsLine(PROJECTS, 4)).toBeNull();
    expect(claudeSessionsLine(PROJECTS, null)).toBeNull();
    expect(optionCountLabel('Initech', 12, 3)).toBe('Initech — 12, в Неразобранном 3');
    expect(optionCountLabel('Initech', 12, 0)).toBe('Initech — 12');
    expect([besed(1), besed(3), besed(12), besed(21), besed(1, true)]).toEqual([
      '1 беседа',
      '3 беседы',
      '12 бесед',
      '21 беседа',
      '1 беседу',
    ]);
    expect(addPreviewLine(5)).toBe('Переложит 5 бесед');
    expect(deletePreviewLine(3)).toBe('Удалить правило? 3 беседы переедут:');
    expect(exampleLine(PROJECTS, { channel_id: 1, title: 'Алиса', from_project_id: null, to_project_id: 4 })).toBe(
      'Алиса: Неразобранное → Личное',
    );
    expect(freeColor(PROJECTS)).toBe(PROJECT_PALETTE[2]);
  });
});

// --- 15: шапка беседы -----------------------------------------------------------------------------------------

describe('Ф14-1 D15: проект беседы в шапке', () => {
  it('смена -> PATCH project_id / null / map_source: auto, затем /sync и проекты', async () => {
    store.state.sync.channelsById[1] = channel({ project_id: 4, map_source: 'auto', map_reason: 'правило #3: весь аккаунт' });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    const select = wrapper.get('select[aria-label="Проект беседы"]');
    expect((select.element as HTMLSelectElement).value).toBe('auto');
    expect(select.findAll('option')[0].text()).toBe('По правилам: Личное');
    expect(wrapper.get('.map-reason').text()).toBe('весь аккаунт');

    await select.setValue('1');
    await flushPromises();
    expect(callsOf('PATCH', '/channels/1').map((c) => c.body)).toEqual([{ project_id: 1 }]);
    expect(callsOf('GET', '/sync').length).toBeGreaterThan(0);
    expect(callsOf('GET', '/projects').length).toBeGreaterThan(0);

    await select.setValue('none');
    await flushPromises();
    expect(callsOf('PATCH', '/channels/1').at(-1)?.body).toEqual({ project_id: null });

    store.state.sync.channelsById[1] = channel({ project_id: null, map_source: 'manual', map_reason: 'manual' });
    await flushPromises();
    expect((select.element as HTMLSelectElement).value).toBe('none');
    expect(wrapper.get('.map-reason').text()).toBe('вручную');
    await select.setValue('auto');
    await flushPromises();
    expect(callsOf('PATCH', '/channels/1').at(-1)?.body).toEqual({ map_source: 'auto' });
  });

  it('строка про сессии Claude — только у проекта с рабочей папкой; в half — в меню', async () => {
    store.state.sync.channelsById[1] = channel({ project_id: 1, map_source: 'manual', map_reason: 'manual' });
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    expect(wrapper.get('.map-claude-line').text()).toBe('беседу видят сессии Claude проекта «Globex»');

    store.state.sync.channelsById[1] = channel({ project_id: 4, map_source: 'manual', map_reason: 'manual' });
    await flushPromises();
    expect(wrapper.find('.map-claude-line').exists()).toBe(false);

    store.state.sync.channelsById[1] = channel({ project_id: 1, map_source: 'manual', map_reason: 'manual' });
    layout.mode = 'half';
    await flushPromises();
    expect(wrapper.get('.feed-actions--menu .map-claude-line').text()).toContain('«Globex»');
    expect(wrapper.find('.feed-actions--menu select[aria-label="Проект беседы"]').exists()).toBe(true);
  });

  it('ошибка — текстом в шапке', async () => {
    handler = (c) => (c.method === 'PATCH' ? jsonResponse({ detail: 'беседа не найдена' }, 404) : undefined);
    store.state.sync.channelsById[1] = channel({});
    const wrapper = mount(ChannelView, { props: { channelId: 1 } });
    await flushPromises();
    await wrapper.get('select[aria-label="Проект беседы"]').setValue('4');
    await flushPromises();
    expect(wrapper.get('.map-error').text()).toContain('беседа не найдена');
    expect((wrapper.get('select[aria-label="Проект беседы"]').element as HTMLSelectElement).value).toBe('auto');
  });
});

// --- 16: выбор бесед в списке ---------------------------------------------------------------------------------

describe('Ф14-1 D16: выбор бесед и перенос пачкой', () => {
  function seed(): void {
    store.state.sync.channelsById = {
      1: channel({ id: 1, title: 'Алиса', last_msg_ts: 3000 }),
      2: channel({ id: 2, title: 'Петя', last_msg_ts: 2000 }),
      3: channel({ id: 3, title: 'Рассылка', attention: 'info', last_msg_ts: 1000 }),
      4: channel({ id: 4, title: 'Старое', archived: true }),
    };
  }

  it('клик по строке в режиме выбора не открывает беседу; «Все» — включая свёрнутую «К сведению»', async () => {
    seed();
    const wrapper = mount(ChannelList);
    await flushPromises();
    expect(wrapper.find('.attention-group--info .channel-button').exists()).toBe(false); // «К сведению» свёрнута
    await wrapper.get('.channel-select-button').trigger('click');
    const row = wrapper.get('.channel-button[data-channel-id="1"]');
    await row.trigger('click');
    await flushPromises();
    expect(store.state.currentChannelId).toBeNull();
    expect(calls.some((c) => c.path.endsWith('/messages'))).toBe(false);
    expect(row.attributes('aria-checked')).toBe('true');
    expect(wrapper.get('.channel-select-count').text()).toBe('Выбрано: 1');
    await row.trigger('click');
    expect(wrapper.get('.channel-select-count').text()).toBe('Выбрано: 0');

    await wrapper.get('.channel-select-all').trigger('click');
    expect(wrapper.get('.channel-select-count').text()).toBe('Выбрано: 3');
  });

  it('перенос зовёт moveChannels с id и целью; успех — выход из режима и «Перенесено N»', async () => {
    seed();
    handler = (c) => (c.path === '/api/v1/channels/move' ? jsonResponse({ moved: 2 }) : undefined);
    const wrapper = mount(ChannelList);
    await flushPromises();
    await wrapper.get('.channel-select-button').trigger('click');
    await wrapper.get('.channel-button[data-channel-id="2"]').trigger('click');
    await wrapper.get('.channel-button[data-channel-id="1"]').trigger('click');
    const moveButton = wrapper.get('.move-button');
    expect(moveButton.attributes('disabled')).toBeDefined(); // цель не выбрана
    await wrapper.get('select[aria-label="Перенести в"]').setValue('4');
    await moveButton.trigger('click');
    await flushPromises();
    expect(callsOf('POST', '/channels/move').map((c) => c.body)).toEqual([{ channel_ids: [1, 2], project_id: 4 }]);
    expect(wrapper.find('.channel-select-bar').exists()).toBe(false);
    expect(wrapper.get('.move-notice').text()).toBe('Перенесено 2');
    expect(callsOf('GET', '/projects').length).toBeGreaterThan(0);
    expect(callsOf('GET', '/sync').length).toBeGreaterThan(0);

    // «По правилам» и «Неразобранное» — `auto` и `null`
    await wrapper.get('.channel-select-button').trigger('click');
    await wrapper.get('.channel-button[data-channel-id="1"]').trigger('click');
    await wrapper.get('select[aria-label="Перенести в"]').setValue('auto');
    await wrapper.get('.move-button').trigger('click');
    await flushPromises();
    expect(callsOf('POST', '/channels/move').at(-1)?.body).toEqual({ channel_ids: [1], project_id: 'auto' });
    await wrapper.get('.channel-select-button').trigger('click');
    await wrapper.get('.channel-button[data-channel-id="2"]').trigger('click');
    await wrapper.get('select[aria-label="Перенести в"]').setValue('none');
    await wrapper.get('.move-button').trigger('click');
    await flushPromises();
    expect(callsOf('POST', '/channels/move').at(-1)?.body).toEqual({ channel_ids: [2], project_id: null });
  });

  it('«Перенесено N» пропадает через 5 с', async () => {
    vi.useFakeTimers();
    seed();
    handler = (c) => (c.path === '/api/v1/channels/move' ? jsonResponse({ moved: 1 }) : undefined);
    const wrapper = mount(ChannelList);
    await flushPromises();
    await wrapper.get('.channel-select-button').trigger('click');
    await wrapper.get('.channel-button[data-channel-id="1"]').trigger('click');
    await wrapper.get('select[aria-label="Перенести в"]').setValue('4');
    await wrapper.get('.move-button').trigger('click');
    await flushPromises();
    expect(wrapper.find('.move-notice').exists()).toBe(true);
    vi.advanceTimersByTime(5_000);
    await flushPromises();
    expect(wrapper.find('.move-notice').exists()).toBe(false);
  });

  it('ошибка — текстом, выбор цел', async () => {
    seed();
    handler = (c) =>
      c.path === '/api/v1/channels/move' ? jsonResponse({ detail: 'беседа 2 не найдена' }, 404) : undefined;
    const wrapper = mount(ChannelList);
    await flushPromises();
    await wrapper.get('.channel-select-button').trigger('click');
    await wrapper.get('.channel-button[data-channel-id="1"]').trigger('click');
    await wrapper.get('.channel-button[data-channel-id="2"]').trigger('click');
    await wrapper.get('select[aria-label="Перенести в"]').setValue('4');
    await wrapper.get('.move-button').trigger('click');
    await flushPromises();
    expect(wrapper.get('.move-error').text()).toContain('беседа 2 не найдена');
    expect(wrapper.get('.channel-select-count').text()).toBe('Выбрано: 2');
    expect(wrapper.get('.channel-button[data-channel-id="1"]').attributes('aria-checked')).toBe('true');
  });

  it('больше 200 — «Перенести» неактивна с подсказкой; смена вкладки — выход из режима', async () => {
    const many: Record<number, Channel> = {};
    for (let id = 1; id <= 201; id++) many[id] = channel({ id, title: `беседа ${id}`, last_msg_ts: id });
    store.state.sync.channelsById = many;
    const wrapper = mount(ChannelList);
    await flushPromises();
    await wrapper.get('.channel-select-button').trigger('click');
    await wrapper.get('.channel-select-all').trigger('click');
    await wrapper.get('select[aria-label="Перенести в"]').setValue('4');
    expect(wrapper.get('.channel-select-count').text()).toBe('Выбрано: 201');
    expect(wrapper.get('.move-button').attributes('disabled')).toBeDefined();
    expect(wrapper.get('.move-hint').text()).toContain('200');

    store.state.currentProjectId = -2;
    await flushPromises();
    expect(wrapper.find('.channel-select-bar').exists()).toBe(false);
  });
});

// --- 17: панель «Раскладка» -----------------------------------------------------------------------------------

describe('Ф14-1 D17: панель «Раскладка»', () => {
  const RULES: MapRule[] = [
    {
      id: 3,
      account_id: 1,
      project_id: 1,
      priority: 0,
      kind: 'folder',
      value: 3,
      description: 'папка «Globex»',
      channels: 12,
    },
    { id: 9, account_id: 1, project_id: 4, priority: 0, kind: 'all', value: null, description: 'весь аккаунт', channels: 1 },
    {
      id: 7,
      account_id: 5,
      project_id: 4,
      priority: 0,
      kind: 'domain',
      value: 'example.com',
      description: 'отправитель @example.com',
      channels: 3,
    },
  ];

  function rulesHandler(extra: Handler = () => undefined): Handler {
    return (c) => {
      const custom = extra(c);
      if (custom !== undefined) return custom;
      if (c.path === '/api/v1/map-rules' && c.method === 'GET') return jsonResponse(RULES);
      if (c.path === '/api/v1/accounts/1/map-options') {
        return jsonResponse({
          account_id: 1,
          source: 'telegram',
          kinds: ['folder', 'all'],
          folders: [{ id: 2, title: 'Initech', channels: 12, unsorted: 3 }],
          labels: [],
          domains: [],
        });
      }
      return undefined;
    };
  }

  it('правила по аккаунтам: заголовок — название аккаунта, «описание → проект», «сейчас N бесед»', async () => {
    handler = rulesHandler();
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const groups = wrapper.findAll('.mapping-account');
    expect(groups.map((g) => g.get('.mapping-account-title').text())).toEqual(['Личный TG', 'Почта ИИП']);
    expect(groups[0].findAll('.mapping-rule-what').map((r) => r.text())).toEqual([
      'папка «Globex» → Globex',
      'весь аккаунт → Личное',
    ]);
    expect(groups[0].findAll('.mapping-rule-count').map((r) => r.text())).toEqual(['сейчас 12 бесед', 'сейчас 1 беседа']);
    expect(groups[1].get('.mapping-rule-what').text()).toBe('отправитель @example.com → Личное');
  });

  it('«Добавить» неактивна до «Проверить» и после правки поля; добавление перечитывает правила', async () => {
    handler = rulesHandler((c) => {
      if (c.path !== '/api/v1/map-rules' || c.method !== 'POST') return undefined;
      const body = c.body as { dry_run: boolean };
      return body.dry_run
        ? jsonResponse({
            dry_run: true,
            description: 'папка «Initech»',
            moved: 3,
            examples: [{ channel_id: 11, title: 'Фронт', from_project_id: null, to_project_id: 4 }],
          })
        : jsonResponse({ rule_id: 12, channels_relaid: 3, priority: 0, description: 'папка «Initech»' });
    });
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const addButton = () => wrapper.get('.mapping-add-submit');
    expect(addButton().attributes('disabled')).toBeDefined();

    await wrapper.get('.mapping-add-account').setValue('1');
    await flushPromises();
    expect(callsOf('GET', '/accounts/1/map-options')).toHaveLength(1);
    expect(wrapper.get('.mapping-add-kind').findAll('option').map((o) => o.text())).toEqual([
      '—',
      'Папка Telegram',
      'Весь аккаунт',
    ]);
    await wrapper.get('.mapping-add-kind').setValue('folder');
    const value = wrapper.get('select.mapping-add-value');
    expect(value.findAll('option')[1].text()).toBe('Initech — 12, в Неразобранном 3');
    await value.setValue('2');
    await wrapper.get('.mapping-add-project').setValue('4');
    expect(addButton().attributes('disabled')).toBeDefined();

    await wrapper.get('.mapping-check').trigger('click');
    await flushPromises();
    expect(callsOf('POST', '/map-rules').map((c) => c.body)).toEqual([
      { account_id: 1, project_id: 4, kind: 'folder', value: 2, dry_run: true },
    ]);
    expect(wrapper.get('.mapping-preview').text()).toContain('Переложит 3 беседы');
    expect(wrapper.get('.mapping-preview').text()).toContain('Фронт: Неразобранное → Личное');
    expect(addButton().attributes('disabled')).toBeUndefined();

    // правка поля сбрасывает предпросмотр
    await wrapper.get('.mapping-add-project').setValue('1');
    expect(addButton().attributes('disabled')).toBeDefined();
    expect(wrapper.find('.mapping-preview').exists()).toBe(false);

    await wrapper.get('.mapping-check').trigger('click');
    await flushPromises();
    const rulesLoadsBefore = callsOf('GET', '/map-rules').length;
    await addButton().trigger('click');
    await flushPromises();
    expect(callsOf('POST', '/map-rules').at(-1)?.body).toEqual({
      account_id: 1,
      project_id: 1,
      kind: 'folder',
      value: 2,
      dry_run: false,
    });
    expect(callsOf('GET', '/map-rules').length).toBe(rulesLoadsBefore + 1);
    expect(callsOf('GET', '/projects').length).toBeGreaterThan(0);
    expect(callsOf('GET', '/sync').length).toBeGreaterThan(0);
    expect(wrapper.get('.access-notice').text()).toContain('#12');
  });

  it('409/422 — текстом detail', async () => {
    handler = rulesHandler((c) =>
      c.path === '/api/v1/map-rules' && c.method === 'POST'
        ? jsonResponse({ detail: 'такое правило уже есть (#9)' }, 409)
        : undefined,
    );
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    await wrapper.get('.mapping-add-account').setValue('1');
    await flushPromises();
    await wrapper.get('.mapping-add-kind').setValue('all');
    await wrapper.get('.mapping-add-project').setValue('4');
    await wrapper.get('.mapping-check').trigger('click');
    await flushPromises();
    expect(callsOf('POST', '/map-rules')[0].body).toEqual({
      account_id: 1,
      project_id: 4,
      kind: 'all',
      value: null,
      dry_run: true,
    });
    expect(wrapper.get('.mapping-error').text()).toBe('такое правило уже есть (#9)');
    expect(wrapper.get('.mapping-add-submit').attributes('disabled')).toBeDefined();
  });

  it('удаление — через предпросмотр: DELETE ?dry_run=true, затем «Да, удалить»', async () => {
    handler = rulesHandler((c) => {
      if (c.method !== 'DELETE') return undefined;
      return c.search.includes('dry_run=true')
        ? jsonResponse({
            dry_run: true,
            description: 'весь аккаунт',
            moved: 1,
            examples: [{ channel_id: 5, title: 'Алиса', from_project_id: 4, to_project_id: null }],
          })
        : jsonResponse({ ok: true, channels_relaid: 1 });
    });
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    await wrapper.get('.mapping-rule[data-rule-id="9"] .mapping-rule-delete').trigger('click');
    await flushPromises();
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => `${c.path}${c.search}`)).toEqual([
      '/api/v1/map-rules/9?dry_run=true',
    ]);
    const confirm = wrapper.get('.mapping-rule[data-rule-id="9"] .mapping-confirm');
    expect(confirm.text()).toContain('Удалить правило? 1 беседа переедет:');
    expect(confirm.text()).toContain('Алиса: Личное → Неразобранное');

    await confirm.get('.mapping-delete-confirm').trigger('click');
    await flushPromises();
    expect(calls.filter((c) => c.method === 'DELETE').map((c) => `${c.path}${c.search}`)).toEqual([
      '/api/v1/map-rules/9?dry_run=true',
      '/api/v1/map-rules/9',
    ]);
    expect(callsOf('GET', '/map-rules')).toHaveLength(2);
    expect(wrapper.find('.mapping-confirm').exists()).toBe(false);
  });

  it('«Отмена» предпросмотра удаления ничего не удаляет', async () => {
    handler = rulesHandler((c) =>
      c.method === 'DELETE' ? jsonResponse({ dry_run: true, description: 'x', moved: 0, examples: [] }) : undefined,
    );
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    await wrapper.get('.mapping-rule[data-rule-id="3"] .mapping-rule-delete').trigger('click');
    await flushPromises();
    await wrapper.get('.mapping-delete-cancel').trigger('click');
    expect(wrapper.find('.mapping-confirm').exists()).toBe(false);
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1);
  });

  it('«Новый проект» зовёт createProject (цвет — первый свободный) и loadProjects', async () => {
    handler = rulesHandler((c) =>
      c.path === '/api/v1/projects' && c.method === 'POST' ? jsonResponse({ project_id: 6 }) : undefined,
    );
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.get('.mapping-new-create').attributes('disabled')).toBeDefined();
    await wrapper.get('.mapping-new-name').setValue('  Дом  ');
    await wrapper.get('.mapping-new-create').trigger('click');
    await flushPromises();
    expect(callsOf('POST', '/projects').map((c) => c.body)).toEqual([{ name: 'Дом', color: PROJECT_PALETTE[2] }]);
    expect(callsOf('GET', '/projects').length).toBeGreaterThan(0);
    expect(wrapper.get('.access-notice').text()).toContain('«Дом»');
  });

  it('проекты: подсказка с номером у проекта без папки; переименование и цвет — PATCH', async () => {
    handler = rulesHandler();
    const wrapper = mount(MappingPanel, { attachTo: document.body });
    await flushPromises();
    const rows = wrapper.findAll('.mapping-project');
    expect(rows.map((r) => r.attributes('data-project-id'))).toEqual(['1', '4']);
    expect(rows[0].get('.mapping-project-workdir').text()).toBe('сессии Claude: есть рабочая папка');
    expect(rows[0].text()).not.toContain('hub project set');
    expect(rows[1].get('.mapping-project-workdir').text()).toBe('сессии Claude: нет');
    expect(rows[1].text()).toContain('hub project set 4 --workdir');

    expect(rows[1].find('.mapping-project-save').exists()).toBe(false);
    await rows[1].get('.mapping-project-name').setValue('Личное и дом');
    await rows[1].get('.mapping-project-save').trigger('click');
    await flushPromises();
    expect(callsOf('PATCH', '/projects/4').map((c) => c.body)).toEqual([{ name: 'Личное и дом' }]);

    await rows[0].get('.mapping-project-color').setValue('#00897b');
    await flushPromises();
    expect(callsOf('PATCH', '/projects/1').map((c) => c.body)).toEqual([{ color: '#00897b' }]);
  });

  it('пункт «Раскладка» в меню «⋯» верхней полосы открывает панель', async () => {
    handler = rulesHandler();
    const wrapper = mount(TopBar);
    await flushPromises();
    expect(wrapper.find('.mapping-panel').exists()).toBe(false);
    await wrapper.get('.top-menu-button').trigger('click');
    await wrapper.get('.mapping-open').trigger('click');
    await flushPromises();
    expect(wrapper.find('.mapping-panel').exists()).toBe(true);
    expect(callsOf('GET', '/map-rules')).toHaveLength(1);
  });
});
