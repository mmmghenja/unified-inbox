// Ф62-6 (раздел G): группы WhatsApp в интерфейсе — чистые функции (карточка, исход по участникам, выбор участника,
// «Выйти» для группы WhatsApp), карточка просьбы сессии, «Ждёт», редьюсер заявок и кадр SSE `group_op`.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Channel, GroupOp, RecipientHit } from '../src/api/types';
import GroupOpCard from '../src/components/GroupOpCard.vue';
import ProjectColumn from '../src/components/ProjectColumn.vue';
import GroupMembersModal from '../src/components/GroupMembersModal.vue';
import GroupLinkModal from '../src/components/GroupLinkModal.vue';
import NewGroupForm from '../src/components/NewGroupForm.vue';
import NewMessageForm from '../src/components/NewMessageForm.vue';
import WaitList from '../src/components/WaitList.vue';
import { applyGroupOp, createGroupOpState } from '../src/store/groupOps';
import { createStaleness } from '../src/store/staleness';
import { connectStream, type EventSourceLike } from '../src/store/stream';
import { store } from '../src/store/instance';
import { createStore } from '../src/store/store';
import { buildBoard } from '../src/util/board';
import {
  groupOpOutcomeLines,
  groupOpQuestion,
  manualPhone,
  participantOfHit,
  visibleGroupOps,
} from '../src/util/groupOps';
import { canLeaveChannel, leaveConfirmText } from '../src/util/leaves';
import { buildWaiting, homeDecisionCount } from '../src/util/waiting';

const NOW = new Date(2026, 9, 16, 15, 0, 0).getTime();

function op(overrides: Partial<GroupOp> = {}): GroupOp {
  return {
    id: 1,
    account_id: 11,
    project_id: 3,
    channel_id: null,
    kind: 'create',
    title: 'Клиенты',
    participants: [
      { i: 0, kind: 'phone', mask: '+7 910 ***-**-12', name: null, channel_id: null },
      { i: 1, kind: 'chat', mask: '', name: 'Петя', channel_id: 9 },
    ],
    reason: 'нужна группа для запуска',
    origin: 'session',
    status: 'pending',
    error: null,
    link: null,
    client_name: 'маркетолог',
    role: 'маркетолог',
    created_ts: NOW - 60_000,
    decided_ts: null,
    done_ts: null,
    updated_ts: NOW - 60_000,
    can_decline: true,
    seq: 10,
    ...overrides,
  };
}

function channel(overrides: Partial<Channel> = {}): Channel {
  return {
    id: 7,
    ext_id: '1203@g.us',
    kind: 'group',
    title: 'Клиенты',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1,
    my_msg_count: 0,
    first_seen_ts: 0,
    unread: 0,
    project_id: 3,
    source: 'whatsapp',
    ...overrides,
  } as Channel;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Ф62-6: тексты и видимость', () => {
  it('вопросы карточки по видам', () => {
    expect(groupOpQuestion(op())).toBe('Создать группу «Клиенты» с: +7 910 ***-**-12, Петя?');
    expect(groupOpQuestion(op({ kind: 'add', channel_id: 7, title: null }), 'Клиенты')).toBe(
      'Добавить в «Клиенты»: +7 910 ***-**-12, Петя?',
    );
    expect(groupOpQuestion(op({ kind: 'remove', channel_id: 7, title: 'Клиенты' }))).toBe(
      'Убрать из «Клиенты»: +7 910 ***-**-12, Петя?',
    );
    expect(groupOpQuestion(op({ kind: 'link_reset', channel_id: 7 }), 'Клиенты')).toBe('Сбросить ссылку «Клиенты»?');
  });

  it('карточкой — только ждущие и одобренные заявки сессий; заявка владельца из интерфейса и ссылка — нет', () => {
    const list = [
      op({ id: 1 }),
      op({ id: 2, status: 'approved' }),
      op({ id: 3, status: 'done' }),
      op({ id: 4, origin: 'ui', status: 'approved' }),
      op({ id: 5, kind: 'link', status: 'approved', channel_id: 7 }),
    ];
    expect(visibleGroupOps(list).map((o) => o.id)).toEqual([1, 2]);
  });

  it('Ф33-3: заявка владельца, возвращённая на решение после восстановления, — карточкой с причиной', () => {
    const held = op({ id: 6, origin: 'ui', status: 'pending', error: 'восстановлено из копии — проверь в WhatsApp' });
    expect(visibleGroupOps([held]).map((o) => o.id)).toEqual([6]);
    const wrapper = mount(GroupOpCard, { props: { op: held } });
    expect(wrapper.get('.group-op-hold').text()).toBe('восстановлено из копии — проверь в WhatsApp');
    expect(wrapper.find('button.group-op-approve').exists()).toBe(true);
    wrapper.unmount();
    const plain = mount(GroupOpCard, { props: { op: op() } });
    expect(plain.find('.group-op-hold').exists()).toBe(false);
    plain.unmount();
  });

  it('итог по участникам словами; пока кодов нет — пусто', () => {
    expect(groupOpOutcomeLines(op())).toEqual([]);
    const done = op({
      status: 'done',
      participants: [
        { i: 0, kind: 'phone', mask: '+7 910 ***-**-12', name: null, channel_id: null, code: 'privacy', invite: true },
        { i: 1, kind: 'chat', mask: '', name: 'Петя', channel_id: 9, code: 'no_whatsapp' },
      ],
    });
    expect(groupOpOutcomeLines(done)).toEqual([
      '+7 910 ***-**-12 — приватность — пошли ссылку',
      'Петя — нет WhatsApp',
    ]);
  });

  it('участник из поиска: личка WhatsApp того же аккаунта -> #id, человек -> номер; чужой аккаунт и группы — нет', () => {
    const dm: RecipientHit = { type: 'channel', channel_id: 9, title: 'Петя', account_id: 11, source: 'whatsapp', kind: 'dm', project_id: 3, last_msg_ts: 1 };
    const grp: RecipientHit = { ...dm, channel_id: 10, kind: 'group' };
    const person: RecipientHit = { type: 'person', account_id: 11, source: 'whatsapp', name: 'Маша', handle: null, to: '+79000004567', channel_id: null };
    expect(participantOfHit(dm, 11)).toEqual({ value: '#9', label: 'Петя' });
    expect(participantOfHit(grp, 11)).toBeNull();
    expect(participantOfHit(person, 11)).toEqual({ value: '+79000004567', label: 'Маша' });
    expect(participantOfHit(dm, 12)).toBeNull();
    expect(participantOfHit({ ...person, source: 'telegram' }, 11)).toBeNull();
  });

  it('номер руками: +7… или 8…, 8–15 цифр', () => {
    expect(manualPhone('+7 900 000-45-67')).toBe('+7 900 000-45-67');
    expect(manualPhone('89000004567')).toBe('89000004567');
    expect(manualPhone('79000004567')).toBeNull();
    expect(manualPhone('Петя')).toBeNull();
    expect(manualPhone('+123')).toBeNull();
  });

  it('«Выйти» разрешено для группы WhatsApp, не для лички; подтверждение называет WhatsApp', () => {
    expect(canLeaveChannel(channel())).toBe(true);
    expect(canLeaveChannel(channel({ kind: 'dm' }))).toBe(false);
    expect(canLeaveChannel(channel({ archived: true }))).toBe(false);
    expect(leaveConfirmText('Клиенты', 'whatsapp')).toContain('в WhatsApp');
    expect(leaveConfirmText('Клиенты')).toContain('в Telegram');
  });
});

describe('Ф62-6: «Ждёт», столбец и стор', () => {
  const HOME = { react: [], drafts: [], runs: [], sent: [] };
  const WORK = { tasks: {}, pending: {}, stale: {} };
  const PROJECT = { id: 3, name: 'Umbrella', color: '#ff0000', sort: 0, muted: false, workdir: '/p', unread: 0 };

  it('заявка сессии попадает во «Ждёт», счётчик главной считает только ждущие решения', () => {
    const input = {
      projects: [PROJECT],
      home: HOME,
      
      nowMs: NOW,
      groupOps: [op({ id: 1 }), op({ id: 2, status: 'approved', can_decline: false })],
    };
    expect(buildWaiting(input as never).filter((i) => i.kind === 'group_op')).toHaveLength(2);
    expect(homeDecisionCount(input as never)).toBe(1);
  });

  it('карточка — в «Нужна реакция» столбца проекта', () => {
    const board = buildBoard([PROJECT], { ...HOME, groupOps: [op()] } as never, NOW);
    const wrapper = mount(ProjectColumn, { props: { column: board.columns[0]! } });
    expect(wrapper.get('[data-section="react"] .board-section-title').text()).toBe('Нужна реакция · 1');
    expect(wrapper.find('[data-section="react"] [data-section="group-ops"]').exists()).toBe(true);
    expect(wrapper.find('button.group-op-approve').exists()).toBe(true);
  });

  it('WaitList рисует карточку', () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } })));
    store.state.groupOps.items[1] = op();
    const wrapper = mount(WaitList);
    expect(wrapper.find('.group-op-card').exists()).toBe(true);
    delete store.state.groupOps.items[1];
    wrapper.unmount();
  });

  it('редьюсер: старший seq побеждает', () => {
    const state = createGroupOpState();
    applyGroupOp(state, op({ seq: 10, status: 'approved' }));
    applyGroupOp(state, op({ seq: 5, status: 'pending' }));
    expect(state.items[1]!.status).toBe('approved');
    applyGroupOp(state, op({ seq: 11, status: 'done' }));
    expect(state.items[1]!.status).toBe('done');
  });

  it('кадр SSE group_op запускает /sync', async () => {
    const listeners = new Map<string, () => void>();
    const es: EventSourceLike = {
      readyState: 1,
      addEventListener: (type: string, fn: () => void) => listeners.set(type, fn),
      close: () => {},
    };
    vi.useFakeTimers();
    const onSyncNeeded = vi.fn();
    const handle = connectStream({ url: '/s', staleness: createStaleness(), onSyncNeeded, createEventSource: () => es });
    listeners.get('group_op')!();
    await vi.advanceTimersByTimeAsync(60);
    expect(onSyncNeeded).toHaveBeenCalledTimes(1);
    handle.close();
    vi.useRealTimers();
  });

  it('/sync.group_ops попадает в стор', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ messages: [], channels: [], max_seq: 11, more: false, group_ops: [op({ seq: 11 })] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    );
    const s = createStore();
    await s.runSync();
    expect(s.state.groupOps.items[1]!.title).toBe('Клиенты');
  });
});

describe('Ф62-6: карточка', () => {
  function fetchMock() {
    const sent: { method: string; path: string }[] = [];
    const fn = vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      sent.push({ method: init?.method ?? 'GET', path: url.pathname });
      return new Response(JSON.stringify(op({ status: 'approved', seq: 20 })), { status: 200, headers: { 'content-type': 'application/json' } });
    });
    return { sent, fn };
  }

  it('вопрос, «Зачем» и от кого; «Да» и «Отказать» зовут approve / decline', async () => {
    const { sent, fn } = fetchMock();
    vi.stubGlobal('fetch', fn);
    const wrapper = mount(GroupOpCard, { props: { op: op() } });
    const text = wrapper.text();
    expect(text).toContain('Создать группу «Клиенты» с: +7 910 ***-**-12, Петя?');
    expect(text).toContain('Зачем: нужна группа для запуска · маркетолог');
    await wrapper.get('button.group-op-approve').trigger('click');
    await flushPromises();
    await wrapper.get('button.group-op-decline').trigger('click');
    await flushPromises();
    const posts = sent.filter((s) => s.method === 'POST').map((s) => s.path);
    expect(posts).toContain('/api/v1/group-ops/1/approve');
    expect(posts).toContain('/api/v1/group-ops/1/decline');
    wrapper.unmount();
  });

  it('одобренная — «создаётся…» без «Да»; «Отказать» только пока can_decline', () => {
    const early = mount(GroupOpCard, { props: { op: op({ status: 'approved' }) } });
    expect(early.text()).toContain('создаётся…');
    expect(early.find('button.group-op-approve').exists()).toBe(false);
    expect(early.find('button.group-op-decline').exists()).toBe(true);
    const running = mount(GroupOpCard, { props: { op: op({ status: 'approved', can_decline: false }) } });
    expect(running.find('button.group-op-decline').exists()).toBe(false);
  });

  it('чужой текст — только текстом; клик по вопросу открывает беседу', async () => {
    const xss = '<img src=x onerror="window.__xss=1">';
    const open = vi.spyOn(store, 'openChannelAt').mockResolvedValue();
    const wrapper = mount(GroupOpCard, { props: { op: op({ kind: 'add', channel_id: 7, title: xss, reason: xss }) } });
    expect(wrapper.find('img').exists()).toBe(false);
    expect(wrapper.text()).toContain(xss);
    await wrapper.get('button.group-op-question--link').trigger('click');
    expect(open).toHaveBeenCalledWith(3, 7);
  });
});

describe('Ф62-6: форма «Новая группа» и окно ссылки', () => {
  const WA = { id: 11, source: 'whatsapp', title: '79000006507', status: 'ok', last_ok_ts: null, last_error: null, flood_until: null, new_message: true };

  function jsonFetch(handler: (path: string, method: string, body: unknown) => unknown) {
    const sent: { method: string; path: string; body: unknown }[] = [];
    const fn = vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const method = init?.method ?? 'GET';
      const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
      sent.push({ method, path: url.pathname, body });
      return new Response(JSON.stringify(handler(url.pathname, method, body) ?? []), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    });
    return { sent, fn };
  }

  it('переключатель «Новая группа» виден только при аккаунте WhatsApp', async () => {
    vi.stubGlobal('fetch', jsonFetch(() => []).fn);
    store.state.status = { accounts: [] } as never;
    const without = mount(NewMessageForm, { props: { projectId: 3 } });
    expect(without.find('.group-mode-switch').exists()).toBe(false);
    store.state.status = { accounts: [WA] } as never;
    const withWa = mount(NewMessageForm, { props: { projectId: 3 } });
    expect(withWa.find('.group-mode-switch').exists()).toBe(true);
    await withWa.findAll('.group-mode-switch button')[1]!.trigger('click');
    expect(withWa.find('.new-group-form').exists()).toBe(true);
    store.state.status = null;
  });

  it('«Создать» шлёт /group-ops/ui; на done открывает беседу группы; название не длиннее 25', async () => {
    const { sent, fn } = jsonFetch((path, method) =>
      path === '/api/v1/group-ops/ui' && method === 'POST' ? op({ id: 5, origin: 'ui', status: 'approved', seq: 30 }) : [],
    );
    vi.stubGlobal('fetch', fn);
    const open = vi.spyOn(store, 'openRecipientChannel').mockResolvedValue(undefined as never);
    const wrapper = mount(NewGroupForm, { props: { projectId: 3, accounts: [WA as never] } });
    const input = wrapper.get('input.new-group-title');
    expect(input.attributes('maxlength')).toBe('25');
    await input.setValue('Клиенты');
    await wrapper.get('input.group-people-input').setValue('+7 900 000-45-67');
    await wrapper.get('button.group-people-option--phone').trigger('click');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    const post = sent.find((s) => s.path === '/api/v1/group-ops/ui');
    expect(post?.body).toEqual({ kind: 'create', project_id: 3, account_id: 11, title: 'Клиенты', participants: ['+7 900 000-45-67'] });
    expect(wrapper.text()).toContain('создаётся…');
    store.state.groupOps.items[5] = op({ id: 5, origin: 'ui', status: 'done', channel_id: 7, seq: 31 });
    await flushPromises();
    expect(open).toHaveBeenCalledWith(3, 7);
    expect(wrapper.emitted('done')?.[0]).toEqual([7]);
    delete store.state.groupOps.items[5];
  });

  it('failed — причина, форма снова доступна', async () => {
    const { fn } = jsonFetch((path, method) =>
      path === '/api/v1/group-ops/ui' && method === 'POST' ? op({ id: 6, origin: 'ui', status: 'approved', seq: 40 }) : [],
    );
    vi.stubGlobal('fetch', fn);
    const wrapper = mount(NewGroupForm, { props: { projectId: null, accounts: [WA as never] } });
    await wrapper.get('input.new-group-title').setValue('Клиенты');
    await wrapper.get('input.group-people-input').setValue('89000004567');
    await wrapper.get('button.group-people-option--phone').trigger('click');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    store.state.groupOps.items[6] = op({ id: 6, origin: 'ui', status: 'failed', error: 'no_participants', seq: 41 });
    await flushPromises();
    expect(wrapper.get('[role="alert"]').text()).toContain('некого добавлять');
    expect(wrapper.get('button.new-message-submit').attributes('disabled')).toBeUndefined();
    delete store.state.groupOps.items[6];
  });

  it('окно ссылки: просит link, показывает ссылку, «Копировать» и «Сбросить» с подтверждением', async () => {
    const { sent, fn } = jsonFetch((path, method, body) => {
      if (path === '/api/v1/group-ops/ui' && method === 'POST') {
        const kind = (body as { kind: string }).kind;
        return op({ id: kind === 'link' ? 8 : 9, kind: kind as never, channel_id: 7, origin: 'ui', status: 'approved', seq: 50 });
      }
      return [];
    });
    vi.stubGlobal('fetch', fn);
    const wrapper = mount(GroupLinkModal, { props: { channelId: 7, title: 'Клиенты' }, attachTo: document.body });
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/group-ops/ui')?.body).toEqual({ kind: 'link', channel_id: 7 });
    store.state.groupOps.items[8] = op({ id: 8, kind: 'link', channel_id: 7, origin: 'ui', status: 'done', link: 'https://chat.whatsapp.com/ABC', seq: 51 });
    await flushPromises();
    const field = document.body.querySelector('input.group-link-field') as HTMLInputElement;
    expect(field.value).toBe('https://chat.whatsapp.com/ABC');
    vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    (document.body.querySelector('button.group-link-reset') as HTMLButtonElement).click();
    await flushPromises();
    expect(sent.filter((s) => (s.body as { kind?: string } | undefined)?.kind === 'link_reset')).toHaveLength(0);
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    (document.body.querySelector('button.group-link-reset') as HTMLButtonElement).click();
    await flushPromises();
    expect(sent.filter((s) => (s.body as { kind?: string } | undefined)?.kind === 'link_reset')).toHaveLength(1);
    wrapper.unmount();
    delete store.state.groupOps.items[8];
  });
});

describe('Ф62-6: живой состав группы', () => {
  const MEMBERS = {
    channel_id: 7,
    members: [
      { jid: '1@lid', name: 'Я сам', phone_masked: '+7 910 ***-**-67', phone: '+79000004567', is_admin: true, is_me: true },
      { jid: '2@lid', name: 'Петя', phone_masked: '+7 911 ***-**-00', phone: '+79000010000', is_admin: false, is_me: false },
    ],
    updated_ts: NOW,
    refresh: null,
  };

  it('«members» нигде не показывается карточкой и не считается', () => {
    expect(visibleGroupOps([op({ id: 1, kind: 'members', origin: 'ui', status: 'approved' }), op({ id: 2, kind: 'members', status: 'pending' })])).toEqual([]);
  });

  it('окно: refresh=1 при открытии, участники с пометками, «Убрать» не у себя, аккаунт — из строки беседы', async () => {
    const sent: { method: string; path: string; query: string; body: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        const url = new URL(input, 'http://localhost');
        const method = init?.method ?? 'GET';
        const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
        sent.push({ method, path: url.pathname, query: url.search, body });
        const data =
          url.pathname === '/api/v1/channels/7/group-members'
            ? MEMBERS
            : url.pathname === '/api/v1/group-ops/ui'
              ? op({ id: 12, kind: 'remove', channel_id: 7, origin: 'ui', status: 'approved', seq: 60 })
              : [];
        return new Response(JSON.stringify(data), { status: 200, headers: { 'content-type': 'application/json' } });
      }),
    );
    store.state.sync.channelsById[7] = channel({ account_id: 11 });
    const wrapper = mount(GroupMembersModal, { props: { channelId: 7, title: 'Клиенты' }, attachTo: document.body });
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/channels/7/group-members')?.query).toBe('?refresh=1');
    const rows = document.body.querySelectorAll('.group-roster-item');
    expect(rows).toHaveLength(2);
    expect(rows[0]!.textContent).toContain('админ');
    expect(rows[0]!.textContent).toContain('я');
    expect(rows[0]!.querySelector('button.group-roster-remove')).toBeNull();
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    (rows[1]!.querySelector('button.group-roster-remove') as HTMLButtonElement).click();
    await flushPromises();
    expect(sent.find((s) => s.path === '/api/v1/group-ops/ui')?.body).toEqual({ kind: 'remove', channel_id: 7, participants: ['+79000010000'] });
    wrapper.unmount();
    delete store.state.sync.channelsById[7];
  });

  it('пока идёт задание и снимка нет — «загружаю…»', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string) => {
        const path = new URL(input, 'http://localhost').pathname;
        const data = path.endsWith('/group-members')
          ? { channel_id: 7, members: [], updated_ts: null, refresh: { op_id: 20, status: 'approved' } }
          : [];
        return new Response(JSON.stringify(data), { status: 200, headers: { 'content-type': 'application/json' } });
      }),
    );
    const wrapper = mount(GroupMembersModal, { props: { channelId: 7, title: 'Клиенты' }, attachTo: document.body });
    await flushPromises();
    expect(document.body.querySelector('.group-roster-loading')?.textContent).toContain('загружаю…');
    wrapper.unmount();
  });
});
