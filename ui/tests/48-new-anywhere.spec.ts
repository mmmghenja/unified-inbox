// Ф14-2 D (`docs/architecture.md`): кнопка «Новое сообщение» — в верхней полосе и в
// «Беседах» телефона, не в списке бесед; форма — «Кому» с поиском (пауза, устаревший ответ отбрасывается), выбор
// беседы открывает её без `channels/new`, человек Telegram -> объект `tg_user` с его аккаунта, люди Slack — чипы
// до 8 -> `slack_users`, «Новый адресат» -> «Откуда» подставлен / выбор обязателен, «По правилам» ->
// `project_id: null`; потребители `new_to` четырёх видов; скрепка в заготовке Telegram и Slack, одна пачка.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type {
  Account,
  Channel,
  NewRecipient,
  Project,
  RecipientChannel,
  RecipientHit,
  RecipientPerson,
  StatusResponse,
  UploadResult,
} from '../src/api/types';
import ChannelList from '../src/components/ChannelList.vue';
import ChannelView from '../src/components/ChannelView.vue';
import Composer from '../src/components/Composer.vue';
import NewMessageForm from '../src/components/NewMessageForm.vue';
import PhoneShell from '../src/components/PhoneShell.vue';
import TopBar from '../src/components/TopBar.vue';
import { store } from '../src/store/instance';
import { FIRST_MESSAGE_MIXED, FIRST_MESSAGE_TOO_MANY, splitFileBatches } from '../src/util/fileBatches';
import { newRecipientLabel, newRecipientWho } from '../src/util/home';
import { initLayout } from '../src/util/layout';
import { SLACK_MAX_PEOPLE, parseNewAddress, pickPerson, type Target } from '../src/util/newMessage';
import { fakeMedia } from './media';

function account(overrides: Partial<Account>): Account {
  return {
    id: 1,
    source: 'telegram',
    title: 'Личный',
    status: 'ok',
    last_ok_ts: null,
    last_error: null,
    flood_until: null,
    new_message: true,
    address: null,
    ...overrides,
  } as Account;
}

function project(id: number, name: string): Project {
  return { id, name, color: null, sort: id, muted: false, workdir: null, unread: 0 };
}

function channel(overrides: Partial<Channel>): Channel {
  return {
    id: 60,
    ext_id: 'new:0123456789abcdef0123456789abcdef',
    kind: 'dm',
    title: 'Пётр',
    tier: null,
    muted: false,
    archived: false,
    map_reason: 'новая беседа',
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: 1000,
    unread: 0,
    project_id: null,
    source: 'telegram',
    can_reply: true,
    new_to: { kind: 'tg_user', user_id: 555, name: 'Пётр', username: null },
    ...overrides,
  };
}

function tgPerson(overrides: Partial<RecipientPerson> = {}): RecipientPerson {
  return {
    type: 'person',
    account_id: 1,
    source: 'telegram',
    name: 'Пётр Иванов',
    handle: '@petr',
    to: { kind: 'tg_user', user_id: 555, name: 'Пётр Иванов', username: 'petr' },
    channel_id: null,
    ...overrides,
  };
}

function slackPerson(id: string, name: string, accountId = 2): RecipientPerson {
  return {
    type: 'person',
    account_id: accountId,
    source: 'slack',
    name,
    handle: null,
    to: { kind: 'slack_users', users: [id] },
    channel_id: null,
  };
}

function channelHit(overrides: Partial<RecipientChannel> = {}): RecipientChannel {
  return {
    type: 'channel',
    channel_id: 77,
    title: 'Пётр (личка)',
    account_id: 1,
    source: 'telegram',
    kind: 'dm',
    project_id: 1,
    last_msg_ts: 2000,
    ...overrides,
  };
}

const TWO_MAIL = [
  account({ id: 1, source: 'telegram', title: 'Личный' }),
  account({ id: 2, source: 'slack', title: 'Globex', new_message: false }),
  account({ id: 3, source: 'mail', title: 'Gmail', address: 'me@gmail.example' }),
  account({ id: 5, source: 'mail', title: 'Почта ИИП', address: 'me@umbrella.example' }),
];

type Handler = (url: URL, init: RequestInit | undefined) => Response | Promise<Response> | undefined;
let handler: Handler = () => undefined;
const calls: string[] = [];
let posted: unknown[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

/** `/recipients` отвечает `hits`, `/channels/new` — `created` (тело запоминается). */
function serve(hits: RecipientHit[], created: Channel = channel({})): void {
  handler = (url, init) => {
    if (url.pathname.endsWith('/recipients')) return jsonResponse(hits);
    if (url.pathname.endsWith('/channels/new')) {
      posted.push(JSON.parse(String(init?.body)));
      return jsonResponse({ channel: created, created: true });
    }
    return undefined;
  };
}

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  calls.length = 0;
  posted = [];
  handler = () => undefined;
  store.state.sync.channelsById = {};
  store.state.sync.threads = {};
  store.state.openThread = null;
  store.state.homeOpen = false;
  store.state.currentProjectId = 3;
  store.state.currentChannelId = null;
  store.state.composerFocusChannelId = null;
  store.state.projects = [project(1, 'Globex'), project(3, 'Umbrella')];
  store.state.status = { accounts: TWO_MAIL } as unknown as StatusResponse;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      calls.push(`${init?.method ?? 'GET'} ${url.pathname}${url.search}`);
      const custom = await handler(url, init);
      if (custom !== undefined) return custom;
      if (url.pathname.endsWith('/messages')) return jsonResponse({ messages: [], next_before: null });
      return jsonResponse([]);
    }),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  initLayout(null);
  document.body.innerHTML = '';
});

function recipientCalls(): string[] {
  return calls.filter((c) => c.includes('/recipients'));
}

/** Ввод в «Кому» и ожидание паузы поиска (настоящие таймеры). */
async function typeTo(w: VueWrapper, value: string): Promise<void> {
  await w.get('.new-message-to').setValue(value);
  await new Promise((resolve) => setTimeout(resolve, 260));
  await flushPromises();
}

async function submit(w: VueWrapper): Promise<void> {
  await w.get('form').trigger('submit');
  await flushPromises();
}

// --- тест 14 -----------------------------------------------------------------------------------------------------

describe('Ф14-2 D1 (тест 14): кнопка «Новое сообщение»', () => {
  it('пункт меню «⋯»; проект по умолчанию — вкладка настоящего проекта, на главной — «По правилам»', async () => {
    const bar = mount(TopBar);
    await flushPromises();
    await bar.get('.top-menu-button').trigger('click');
    await bar.get('.shell-status .new-message-button').trigger('click');
    expect(bar.findComponent(NewMessageForm).exists()).toBe(true);
    expect((bar.get('.new-message-project').element as HTMLSelectElement).value).toBe('3');
    await bar.get('.new-message-form').trigger('keydown', { key: 'Escape' });
    expect(bar.findComponent(NewMessageForm).exists()).toBe(false);

    store.state.homeOpen = true;
    await flushPromises();
    await bar.get('.top-menu-button').trigger('click');
    await bar.get('.new-message-button').trigger('click');
    expect((bar.get('.new-message-project').element as HTMLSelectElement).value).toBe('');
    expect(bar.get('.new-message-project option').text()).toBe('По правилам');
    bar.unmount();
  });

  it('в списке бесед кнопки больше нет (режим выбора бесед на месте)', async () => {
    store.state.sync.channelsById = { 9: channel({ id: 9, ext_id: 'c9', new_to: null, project_id: 3 }) };
    const list = mount(ChannelList);
    await flushPromises();
    expect(list.find('.new-message-button').exists()).toBe(false);
    expect(list.find('.channel-select-button').exists()).toBe(true);
    list.unmount();
  });

  it('телефон: в «Беседах» над чипами проектов, форма на весь экран', async () => {
    initLayout(fakeMedia(390).matchMedia);
    store.state.phoneTab = 'chats';
    const shell = mount(PhoneShell);
    await flushPromises();
    const head = shell.get('.phone-chats-head');
    const button = head.get('.new-message-button');
    const chips = head.get('.project-chips');
    expect(button.element.compareDocumentPosition(chips.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await button.trigger('click');
    expect(shell.find('.new-message-form--phone').exists()).toBe(true);
    shell.unmount();
  });
});

// --- тест 15 -----------------------------------------------------------------------------------------------------

describe('Ф14-2 D2 (тест 15): поиск «Кому»', () => {
  it('пауза 250 мс, от двух символов, один запрос на серию ввода', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    serve([]);
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    await w.get('.new-message-to').setValue('П');
    vi.advanceTimersByTime(1000);
    await flushPromises();
    expect(recipientCalls()).toEqual([]);

    await w.get('.new-message-to').setValue('Пе');
    vi.advanceTimersByTime(100);
    await w.get('.new-message-to').setValue('Пет');
    vi.advanceTimersByTime(249);
    await flushPromises();
    expect(recipientCalls()).toEqual([]);
    vi.advanceTimersByTime(1);
    await flushPromises();
    expect(recipientCalls()).toEqual([`GET /api/v1/recipients?q=${encodeURIComponent('Пет')}`]);
    w.unmount();
  });

  it('устаревший ответ не перетирает новый', async () => {
    const pending: Record<string, (r: Response) => void> = {};
    handler = (url) => {
      if (!url.pathname.endsWith('/recipients')) return undefined;
      const q = url.searchParams.get('q') ?? '';
      return new Promise<Response>((resolve) => {
        pending[q] = resolve;
      });
    };
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    await typeTo(w, 'ма');
    await typeTo(w, 'мар');
    pending['мар']!(jsonResponse([channelHit({ channel_id: 2, title: 'Марина' })]));
    await flushPromises();
    pending['ма']!(jsonResponse([channelHit({ channel_id: 1, title: 'Globex' })]));
    await flushPromises();
    expect(w.findAll('.new-message-suggest-channel').map((b) => b.attributes('data-channel-id'))).toEqual(['2']);
    w.unmount();
  });

  it('выбор беседы открывает её, заготовка не заводится', async () => {
    serve([channelHit({ channel_id: 77, project_id: 1 }), tgPerson()]);
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    await typeTo(w, 'Пётр');
    expect(w.findAll('.new-message-suggest-title').map((t) => t.text())).toEqual(['Беседы', 'Люди']);
    await w.get('.new-message-suggest-channel').trigger('click');
    await flushPromises();
    expect(w.emitted('done')).toEqual([[77]]);
    expect(calls.some((c) => c.includes('/channels/new'))).toBe(false);
    expect(store.state.currentProjectId).toBe(1);
    expect(store.state.currentChannelId).toBe(77);
    w.unmount();
  });

  it('человек Telegram -> channels/new с объектом tg_user и его аккаунтом; «По правилам» -> project_id null', async () => {
    const person = tgPerson();
    serve([person], channel({ id: 60, project_id: null }));
    const w = mount(NewMessageForm, { props: { projectId: null } });
    await typeTo(w, 'Пётр');
    await w.get('.new-message-suggest-person').trigger('click');
    expect(w.get('.new-message-from-fixed').text()).toBe('Telegram · Личный');
    expect(w.find('.new-message-account').exists()).toBe(false);
    expect(w.find('.new-message-subject').exists()).toBe(false);
    expect((w.get('.new-message-project').element as HTMLSelectElement).value).toBe('');
    await submit(w);
    expect(posted).toEqual([{ account_id: 1, project_id: null, to: person.to }]);
    expect(w.emitted('done')).toEqual([[60]]);
    // без проекта и без правила — вкладка «Неразобранное», беседа открыта, фокус ждёт поле ввода
    expect(store.state.currentProjectId).toBe(-1);
    expect(store.state.currentChannelId).toBe(60);
    expect(store.state.composerFocusChannelId).toBe(60);
    w.unmount();
  });

  it('люди Slack — чипами одного аккаунта, крестик убирает; объект slack_users; проект выбран — число', async () => {
    const people = [slackPerson('U1', 'Аня'), slackPerson('U2', 'Боря'), slackPerson('U3', 'Вера')];
    serve(people, channel({ id: 61, source: 'slack', project_id: 1 }));
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    for (const index of [0, 1, 2]) {
      await typeTo(w, 'ан');
      await w.findAll('.new-message-suggest-person')[index]!.trigger('click');
    }
    expect(w.findAll('.new-message-chip').map((c) => c.attributes('data-slack-id'))).toEqual(['U1', 'U2', 'U3']);
    await w.get('[data-slack-id="U2"] .new-message-chip-remove').trigger('click');
    expect(w.get('.new-message-from-fixed').text()).toBe('Slack · Globex');
    await w.get('.new-message-project').setValue('1');
    await submit(w);
    expect(posted).toEqual([{ account_id: 2, project_id: 1, to: { kind: 'slack_users', users: ['U1', 'U3'] } }]);
    w.unmount();
  });

  it('Slack: не больше 8 человек, повтор не добавляется, другой аккаунт начинает заново', () => {
    let target: Target | null = null;
    for (let i = 1; i <= 9; i += 1) target = pickPerson(target, slackPerson(`U${i}`, `Человек ${i}`));
    expect(target?.type === 'slack' && target.people.map((p) => p.id)).toEqual(
      Array.from({ length: SLACK_MAX_PEOPLE }, (_, i) => `U${i + 1}`),
    );
    target = pickPerson(null, slackPerson('U1', 'Аня'));
    target = pickPerson(target, slackPerson('U1', 'Аня'));
    expect(target.type === 'slack' && target.people).toHaveLength(1);
    target = pickPerson(target, slackPerson('U9', 'Яна', 7));
    expect(target).toEqual({ type: 'slack', accountId: 7, people: [{ id: 'U9', name: 'Яна' }] });
    expect(pickPerson(target, tgPerson()).type).toBe('person');
  });

  it('«Новый адресат» @name: один Telegram-аккаунт — «Откуда» подставлен', async () => {
    serve([], channel({ id: 62, project_id: 3, new_to: { kind: 'tg_username', username: 'petr_new' } }));
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    await typeTo(w, 't.me/petr_new');
    expect(w.get('.new-message-suggest-new').text()).toBe('Telegram: написать @petr_new');
    await w.get('.new-message-suggest-new').trigger('click');
    expect(w.findAll('.new-message-account option').map((o) => o.text())).toEqual(['Telegram · Личный']);
    expect((w.get('.new-message-account').element as HTMLSelectElement).value).toBe('1');
    await submit(w);
    expect(posted).toEqual([{ account_id: 1, project_id: 3, to: '@petr_new' }]);
    w.unmount();
  });

  it('«Новый адресат» почта с двумя ящиками — выбор обязателен, тема обязательна', async () => {
    serve([], channel({ id: 63, source: 'mail', kind: 'mail_thread', project_id: 3 }));
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    await typeTo(w, 'client@example.com');
    expect(w.get('.new-message-suggest-new').text()).toBe('Письмо на client@example.com');
    await w.get('.new-message-suggest-new').trigger('click');
    const select = w.get('.new-message-account');
    expect((select.element as HTMLSelectElement).selectedOptions[0]?.disabled).toBe(true); // «— выберите —»
    expect(w.findAll('.new-message-account option:not([disabled])').map((o) => o.text())).toEqual([
      'me@gmail.example',
      'me@umbrella.example',
    ]);
    await w.get('.new-message-subject').setValue('Подпись');
    expect(w.get('.new-message-submit').attributes('disabled')).toBeDefined(); // ящик не выбран
    await submit(w);
    expect(posted).toEqual([]);
    await select.setValue('5');
    expect(w.get('.new-message-submit').attributes('disabled')).toBeUndefined();
    await w.get('.new-message-subject').setValue('');
    expect(w.get('.new-message-submit').attributes('disabled')).toBeDefined(); // темы нет
    await w.get('.new-message-subject').setValue(' Подпись ');
    await submit(w);
    expect(posted).toEqual([{ account_id: 5, project_id: 3, to: 'client@example.com', subject: 'Подпись' }]);
    w.unmount();
  });

  it('отказ ядра 422 — текстом под формой, форма открыта', async () => {
    handler = (url) => {
      if (url.pathname.endsWith('/recipients')) return jsonResponse([slackPerson('U1', 'Аня')]);
      if (url.pathname.endsWith('/channels/new')) return jsonResponse({ detail: 'user_not_found' }, 422);
      return undefined;
    };
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    await typeTo(w, 'ан');
    await w.get('.new-message-suggest-person').trigger('click');
    await submit(w);
    expect(w.get('.new-message-error').text()).toBe('user_not_found');
    expect(w.emitted('done')).toBeUndefined();
    expect(store.state.currentChannelId).toBeNull();
    w.unmount();
  });

  it('имена и названия из выдачи — только текстом', async () => {
    const payload = '<img src=x onerror=alert(1)>';
    serve([channelHit({ title: payload }), tgPerson({ name: payload, handle: null })]);
    const w = mount(NewMessageForm, { props: { projectId: 3 } });
    await typeTo(w, 'img');
    expect(w.find('.new-message-suggest img').exists()).toBe(false);
    expect(w.get('.new-message-suggest-channel').text()).toContain(payload);
    expect(w.get('.new-message-suggest-person').text()).toContain(payload);
    w.unmount();
  });

  it('разбор «Нового адресата»', () => {
    expect(parseNewAddress('@petr')).toEqual({ kind: 'telegram', username: 'petr' });
    expect(parseNewAddress(' https://t.me/Petr_1 ')).toEqual({ kind: 'telegram', username: 'Petr_1' });
    expect(parseNewAddress('@ab')).toBeNull(); // короче 4
    expect(parseNewAddress('@1petr')).toBeNull(); // не с буквы
    expect(parseNewAddress('Пётр')).toBeNull(); // просто имя — это поиск
    expect(parseNewAddress('a@b.co')).toEqual({ kind: 'mail', address: 'a@b.co' });
    expect(parseNewAddress('a@b')).toBeNull();
    expect(parseNewAddress('Пётр <a@b.co>')).toBeNull();
  });
});

// --- тест 16 -----------------------------------------------------------------------------------------------------

describe('Ф14-2 D3/D4 (тест 16): потребители new_to и скрепка', () => {
  const kinds: [NewRecipient, string, string][] = [
    [{ kind: 'tg_username', username: 'petr' }, '@petr (новая беседа)', '@petr'],
    [
      { kind: 'email', to: 'a@b.co', subject: 'Тема', from: null },
      'a@b.co · Тема',
      'a@b.co · Тема',
    ],
    [{ kind: 'tg_user', user_id: 555, name: 'Пётр', username: 'petr' }, 'Пётр (новая беседа)', 'Пётр'],
    [
      { kind: 'slack_users', users: ['U1', 'U2'], names: ['Аня', 'Боря'] },
      'Аня, Боря (новая беседа)',
      'Аня, Боря',
    ],
  ];

  it.each(kinds)('%o -> подписи', (newTo, label, who) => {
    expect(newRecipientLabel(newTo)).toBe(label);
    expect(newRecipientWho(newTo)).toBe(who);
  });

  it('tg_user без имени — @username, без него — id; неизвестный вид — null', () => {
    expect(newRecipientWho({ kind: 'tg_user', user_id: 5, name: null, username: 'petr' })).toBe('@petr');
    expect(newRecipientWho({ kind: 'tg_user', user_id: 5, name: null, username: null })).toBe('id 5');
    expect(newRecipientWho({ kind: 'slack_users', users: ['U1'], names: [] })).toBe('U1');
    const unknown = { kind: 'future_kind', phone: '+7' } as unknown as NewRecipient;
    expect(newRecipientLabel(unknown)).toBeNull();
    expect(newRecipientWho(unknown)).toBeNull();
  });

  it('строка «кому» в ленте: tg_user и slack_users; неизвестный вид — без строки', async () => {
    store.state.sync.channelsById = {
      60: channel({}),
      61: channel({
        id: 61,
        source: 'slack',
        kind: 'group',
        new_to: { kind: 'slack_users', users: ['U1', 'U2'], names: ['Аня', 'Боря'] },
      }),
      62: channel({ id: 62, new_to: { kind: 'tg_phone' } as unknown as NewRecipient }),
    };
    const expected: [number, string | null][] = [
      [60, 'Новая беседа · кому: Пётр'],
      [61, 'Новая беседа · кому: Аня, Боря'],
      [62, null],
    ];
    for (const [id, line] of expected) {
      const view = mount(ChannelView, { props: { channelId: id } });
      await flushPromises();
      if (line === null) expect(view.find('.new-recipient-line').exists()).toBe(false);
      else expect(view.get('.new-recipient-line').text()).toBe(line);
      expect(view.find('[aria-label="Прикрепить"]').exists()).toBe(true); // скрепка в заготовке
      view.unmount();
    }
  });

  it('заготовка Slack: три файла — один send; 11-й файл не прикрепляется, подсказка', async () => {
    let n = 0;
    vi.spyOn(store, 'uploadFile').mockImplementation(async (_c: number, _f: Blob, name: string) => {
      n += 1;
      return { upload_id: `u${n}`, size: 10, mime: 'x/y', name, kind: 'photo' } satisfies UploadResult;
    });
    const sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined);
    store.state.sync.channelsById = {
      61: channel({ id: 61, source: 'slack', new_to: { kind: 'slack_users', users: ['U1'], names: ['Аня'] } }),
    };
    const w = mount(Composer, { props: { channelId: 61 }, attachTo: document.body });
    const pick = async (count: number) => {
      const input = w.get('input[type=file]');
      const files = Array.from({ length: count }, (_, i) => new File(['x'], `p${i}.png`, { type: 'image/png' }));
      Object.defineProperty(input.element, 'files', { value: files, configurable: true });
      await input.trigger('change');
      await flushPromises();
    };
    await pick(3);
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect((sendSpy.mock.calls[0]![3] as { files: string[] }).files).toEqual(['u1', 'u2', 'u3']);

    await pick(11);
    expect(w.findAll('.composer-file')).toHaveLength(10);
    expect(w.text()).toContain(FIRST_MESSAGE_TOO_MANY);
    w.unmount();
  });

  it('заготовка Telegram: картинка и PDF — ошибка до отправки (одним альбомом нельзя), send не зовётся', async () => {
    let n = 0;
    vi.spyOn(store, 'uploadFile').mockImplementation(async (_c: number, _f: Blob, name: string) => {
      n += 1;
      const kind = name.endsWith('.png') ? 'photo' : 'document';
      return { upload_id: `u${n}`, size: 10, mime: 'x/y', name, kind } satisfies UploadResult;
    });
    const sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined);
    store.state.sync.channelsById = { 60: channel({}) };
    const w = mount(Composer, { props: { channelId: 60 }, attachTo: document.body });
    const input = w.get('input[type=file]');
    const files = [new File(['x'], 'a.png', { type: 'image/png' }), new File(['x'], 'b.pdf', { type: 'application/pdf' })];
    Object.defineProperty(input.element, 'files', { value: files, configurable: true });
    await input.trigger('change');
    await flushPromises();
    await w.get('button[type=submit]').trigger('click');
    await flushPromises();
    expect(sendSpy).not.toHaveBeenCalled();
    expect(w.text()).toContain(FIRST_MESSAGE_MIXED);
    w.unmount();
  });

  it('splitFileBatches для заготовки — одна пачка или ошибка до отправки', () => {
    const item = (kind: string, size = 10) => ({ kind, size });
    const three = [item('photo'), item('photo'), item('photo')];
    expect(splitFileBatches(three, 'telegram', { single: true })).toEqual({ batches: [three] });
    const mixed = [item('photo'), item('document')];
    expect(splitFileBatches(mixed, 'telegram', { single: true })).toEqual({ error: FIRST_MESSAGE_MIXED });
    expect(splitFileBatches(mixed, 'slack', { single: true })).toEqual({ batches: [mixed] });
    const eleven = Array.from({ length: 11 }, () => item('document'));
    expect(splitFileBatches(eleven, 'slack', { single: true })).toEqual({ error: FIRST_MESSAGE_TOO_MANY });
    expect('error' in splitFileBatches([item('document', 30 * 1024 * 1024), item('document', 30 * 1024 * 1024)], 'slack', { single: true })).toBe(true);
    // обычная беседа — как раньше, пачками
    expect(splitFileBatches(eleven, 'slack')).toEqual({ batches: [eleven.slice(0, 10), eleven.slice(10)] });
  });
});
