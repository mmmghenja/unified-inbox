// Ф40-4 A (`docs/architecture.md`): беседа Mattermost в интерфейсе ведёт себя как беседа Slack, кроме
// трёх отличий — текст в Markdown, упоминание `@ник` вместо `<@U…>`, свой набор реакций. Тесты по пунктам A1–A7.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import * as api from '../src/api/client';
import type { Account, Channel, MentionPerson, Message, OutboxListItem, RecipientPerson } from '../src/api/types';
import Composer from '../src/components/Composer.vue';
import DraftCard from '../src/components/DraftCard.vue';
import MessageRow from '../src/components/MessageRow.vue';
import { displayFor, MEDIA_SOURCES, sourceLabel as mediaSourceLabel } from '../src/render/media';
import { ALL_REACTIONS, QUICK_REACTIONS, isReactionSource, moreReactionsFor } from '../src/render/reactions';
import { store } from '../src/store/instance';
import { opensThreadOnReply } from '../src/store/threads';
import { newRecipientLabel } from '../src/util/home';
import { canEdit, canRevoke } from '../src/util/msgOps';
import {
  MATTERMOST_MAX_PEOPLE,
  accountLabel,
  buildChannelNew,
  fromChoice,
  pickPerson,
  removeSlackPerson,
  targetSource,
  type Target,
} from '../src/util/newMessage';
import { sourceBadge, sourceLabel } from '../src/util/people';
import { ruleSourceName } from '../src/util/ruleRequests';
import { isMarkdownSource } from '../src/util/textFormat';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1, seq: 1, first_seq: 1, channel_id: 5, project_id: 1, ext_id: 'abc123', thread_ext_id: null,
    thread_broadcast: false, purged: false, thread: null, reply_to_ext_id: null, grouped_id: null, is_me: false,
    kind: 'text', text: 'привет', entities_json: null, attachments_json: null, forward_json: null,
    mentions_me: false, refs_json: null, reactions_json: null, ts: 1000, edited_ts: null, deleted: false,
    outbox_id: null, outbox_status: null, sender: { id: 1, name: 'Коллега', handle: null },
    ...overrides,
  };
}

function channel(source: string, id = 5): Channel {
  return {
    id, ext_id: 'c5', kind: 'group', title: 'Беседа', tier: null, muted: false, archived: false, map_reason: null,
    last_msg_ts: 1000, my_msg_count: 0, first_seen_ts: 0, unread: 0, project_id: 1, source,
  };
}

function withSource(source: string): void {
  store.state.sync.channelsById = { 5: channel(source) };
  store.state.sync.messagesByChannel = { 5: [] };
}

const MARKDOWN = [
  '**жирный** и `код`',
  '',
  '- пункт один',
  '- пункт два',
  '',
  '| a | b |',
  '|---|---|',
  '| 1 | 2 |',
  '',
  '[сайт](https://example.com/x) и [плохая](javascript:alert(1))',
].join('\n');

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('A1: текст беседы Mattermost — Markdown', () => {
  it('жирный, код, список, таблица, ссылка — разметкой; javascript: — без ссылки', () => {
    withSource('mattermost');
    const w = mount(MessageRow, { props: { message: msg({ text: MARKDOWN }) } });
    const body = w.get('.msg-text--md');
    expect(body.find('strong').text()).toBe('жирный');
    expect(body.find('code').text()).toBe('код');
    expect(body.findAll('li').map((li) => li.text())).toEqual(['пункт один', 'пункт два']);
    expect(body.find('table td').exists()).toBe(true);
    const links = body.findAll('a');
    expect(links.map((a) => a.attributes('href'))).toEqual(['https://example.com/x']);
    expect(links[0]!.attributes('rel')).toContain('noopener');
  });

  it('сырой HTML и картинки не попадают в разметку', () => {
    withSource('mattermost');
    const text = '<script>alert(1)</script><img src=x onerror=alert(1)> ![p](https://e.com/a.png) <b>x</b>';
    const w = mount(MessageRow, { props: { message: msg({ text }) } });
    expect(w.find('script').exists()).toBe(false);
    expect(w.find('img').exists()).toBe(false);
    expect(w.find('.msg-text--md b').exists()).toBe(false);
    expect(w.get('.msg-text--md').text()).toContain('<script>');
  });

  it('невидимый пробел после «@» остаётся в тексте как есть', () => {
    withSource('mattermost');
    const w = mount(MessageRow, { props: { message: msg({ text: '@​channel — не будет' }) } });
    expect(w.get('.msg-text--md').text()).toContain('@​channel');
  });

  it('Telegram и Slack — как раньше: звёздочки остаются текстом, без блока Markdown', () => {
    for (const source of ['telegram', 'slack']) {
      withSource(source);
      const w = mount(MessageRow, { props: { message: msg({ text: '**жирный**' }) } });
      expect(w.find('.msg-text--md').exists()).toBe(false);
      expect(w.get('.msg-text').text()).toBe('**жирный**');
    }
  });

  it('история правок беседы Mattermost — тоже Markdown', async () => {
    withSource('mattermost');
    vi.spyOn(store, 'loadEdits').mockResolvedValue([
      { replaced_ts: 900, text: 'было **так**', entities_json: null },
    ]);
    const w = mount(MessageRow, { props: { message: msg({ id: 11, edited_ts: 950, edit_count: 1 }) } });
    await w.get('button.edited-toggle').trigger('click');
    await flushPromises();
    expect(w.get('.edit-version .msg-text--md strong').text()).toBe('так');
  });

  it('закреплённая карточка черновика в беседу Mattermost показывает Markdown; в поле правки — сырой', async () => {
    const item: OutboxListItem = {
      id: 9, channel_id: 5, project_id: 1, channel_title: 'Беседа', text: 'привет **всем**',
      status: 'pending_approval', origin: 'api', error: null, attempts: 0, created_ts: 0, sent_ts: null,
      ext_id: null, source: 'mattermost', kind: 'group', author: 'mcp:all', reply_to: null, thread: null,
      paced: false, not_before_ts: null, account_id: 7, files: [],
    };
    const w = mount(DraftCard, { props: { item } });
    expect(w.get('.draft-card-text strong').text()).toBe('всем');
    await w.get('.draft-edit').trigger('click');
    expect((w.get('textarea.draft-card-edit').element as HTMLTextAreaElement).value).toBe('привет **всем**');
    const tg = mount(DraftCard, { props: { item: { ...item, source: 'telegram' } } });
    expect(tg.find('.draft-card-text strong').exists()).toBe(false);
    expect(tg.get('.draft-card-text').text()).toBe('привет **всем**');
  });

  it('isMarkdownSource: только mattermost', () => {
    expect(isMarkdownSource('mattermost')).toBe(true);
    for (const s of ['slack', 'telegram', 'mail', '', null, undefined]) expect(isMarkdownSource(s)).toBe(false);
  });
});

describe('A2: «Ответить» в канале Mattermost открывает тред', () => {
  it('сообщение-ответ -> тред родителя; top-level -> свой; плашка ответа не эмитится', async () => {
    withSource('mattermost');
    const spy = vi.spyOn(store, 'openThread').mockResolvedValue();
    const reply = mount(MessageRow, { props: { message: msg({ ext_id: 'r1', thread_ext_id: 'p1' }) } });
    await reply.get('.reply-button').trigger('click');
    expect(spy).toHaveBeenLastCalledWith(5, 'p1');
    expect(reply.emitted('reply')).toBeUndefined();
    const top = mount(MessageRow, { props: { message: msg({ ext_id: 'p1' }) } });
    await top.get('.reply-button').trigger('click');
    expect(spy).toHaveBeenLastCalledWith(5, 'p1');
  });

  it('opensThreadOnReply: Slack и Mattermost — да, Telegram и почта — нет', () => {
    expect(opensThreadOnReply('slack')).toBe(true);
    expect(opensThreadOnReply('mattermost')).toBe(true);
    for (const s of ['telegram', 'mail', undefined, null]) expect(opensThreadOnReply(s)).toBe(false);
  });
});

describe('A3: подсказка «@» в поле ввода Mattermost', () => {
  const ANNA: MentionPerson = { id: 'u1', name: 'Анна Лим', handle: 'anna.lim' };
  const NOBODY: MentionPerson = { id: 'u2', name: 'Без Ника', handle: null };
  let wrapper: VueWrapper | null = null;
  let sendSpy: MockInstance;
  let peopleSpy: MockInstance;

  async function type(w: VueWrapper, value: string): Promise<void> {
    const area = w.get('textarea').element as HTMLTextAreaElement;
    area.value = value;
    area.setSelectionRange(value.length, value.length);
    await w.get('textarea').trigger('input');
  }

  beforeEach(() => {
    vi.useFakeTimers();
    api.setCsrf('c');
    vi.spyOn(api, 'getChannelAccountId').mockResolvedValue(7);
    peopleSpy = vi.spyOn(api, 'searchMentionPeople').mockResolvedValue([ANNA, NOBODY]);
    sendSpy = vi.spyOn(store, 'send').mockResolvedValue(undefined as never);
    withSource('mattermost');
    store.state.sync.channelsById = { 1: channel('mattermost', 1) };
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    vi.useRealTimers();
  });

  it('выбор вставляет @ник как есть (ник из ядра — без @), человек без ника в подсказке не показан, уходит без <@…>', async () => {
    wrapper = mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
    await type(wrapper, 'привет @ан');
    await vi.advanceTimersByTimeAsync(250);
    await flushPromises();
    expect(peopleSpy).toHaveBeenCalledWith(7, 'ан', 1);
    const items = wrapper.findAll('.mention-suggest-item');
    expect(items.map((i) => i.find('.mention-suggest-name').text())).toEqual(['Анна Лим']);
    expect(wrapper.get('.mention-suggest').attributes('aria-label')).toBe('Люди Mattermost');
    await items[0]!.trigger('click');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('привет @anna.lim ');
    await type(wrapper, 'привет @anna.lim глянь');
    await wrapper.get('form').trigger('submit');
    await flushPromises();
    expect(sendSpy.mock.calls[0]![1]).toBe('привет @anna.lim глянь');
  });
});

describe('A4: правка и удаление своих сообщений Mattermost', () => {
  it('своё сообщение с ext_id — «Изменить» и «Удалить у всех»; чужое — нет; заготовка new: — нет', () => {
    withSource('mattermost');
    const own = msg({ is_me: true });
    expect(canEdit(own, channel('mattermost'))).toBe(true);
    expect(canRevoke(own, channel('mattermost'))).toBe(true);
    expect(canEdit(msg({ is_me: false }), channel('mattermost'))).toBe(false);
    expect(canEdit(own, { ...channel('mattermost'), ext_id: 'new:0123' })).toBe(false);
    const w = mount(MessageRow, { props: { message: own } });
    expect(w.findAll('.message-meta button').map((b) => b.text())).toContain('Изменить');
    expect(w.find('button.delete-menu-button').exists()).toBe(true);
  });
});

describe('A5: реакции Mattermost — набор Slack без четырёх эмодзи', () => {
  const MISSING = ['🫡', '🫶', '🫣', '❤️‍🔥'];

  it('mattermost — источник реакций; ряды: быстрый как у Slack, полный без четырёх, быстрый ⊂ полного', () => {
    expect(isReactionSource('mattermost')).toBe(true);
    expect(QUICK_REACTIONS.mattermost).toEqual(QUICK_REACTIONS.slack);
    const key = (e: string): string => e.replace(/️/g, '');
    const mm = new Set(ALL_REACTIONS.mattermost.map(key));
    for (const emoji of MISSING) expect(mm.has(key(emoji))).toBe(false);
    for (const emoji of ALL_REACTIONS.slack) if (!MISSING.map(key).includes(key(emoji))) expect(mm.has(key(emoji))).toBe(true);
    expect(ALL_REACTIONS.mattermost).toHaveLength(ALL_REACTIONS.slack.length - MISSING.length);
    for (const emoji of QUICK_REACTIONS.mattermost) expect(ALL_REACTIONS.mattermost).toContain(emoji);
    expect(moreReactionsFor('mattermost', []).some((e) => MISSING.map(key).includes(key(e)))).toBe(false);
  });

  it('у сообщения Mattermost есть кнопка реакции; клик по быстрому ставит через стор', async () => {
    withSource('mattermost');
    const set = vi.spyOn(store, 'setReaction').mockResolvedValue('queued');
    const w = mount(MessageRow, { props: { message: msg({ id: 7 }) } });
    await w.get('button.react-button').trigger('click');
    const picks = w.findAll('button.reaction-pick');
    expect(picks.length).toBeGreaterThan(0);
    await picks[0]!.trigger('click');
    await flushPromises();
    expect(set).toHaveBeenCalledWith(7, QUICK_REACTIONS.mattermost[0], true);
  });
});

describe('A6: «Новое сообщение» с человеком Mattermost', () => {
  const person = (id: string, name: string, accountId = 7): RecipientPerson => ({
    type: 'person', account_id: accountId, source: 'mattermost', name, handle: `@${name}`,
    to: { kind: 'mm_users', users: [id] }, channel_id: null,
  });

  it('люди одного аккаунта — чипами до 7, без повторов; другой аккаунт заменяет; крестик убирает', () => {
    let target: Target | null = null;
    for (let i = 1; i <= MATTERMOST_MAX_PEOPLE + 2; i += 1) target = pickPerson(target, person(`u${i}`, `n${i}`));
    expect(target).toMatchObject({ type: 'mattermost', accountId: 7 });
    expect(target!.type === 'mattermost' && target!.people).toHaveLength(7);
    target = pickPerson(target, person('u1', 'n1'));
    expect(target.type === 'mattermost' && target.people).toHaveLength(7);
    const other = pickPerson(target, person('x', 'x', 9));
    expect(other).toMatchObject({ type: 'mattermost', accountId: 9 });
    expect(removeSlackPerson(target, 'u1')).toMatchObject({ type: 'mattermost' });
    expect(removeSlackPerson(pickPerson(null, person('u1', 'n1')), 'u1')).toBeNull();
  });

  it('адресат — mm_users со всеми выбранными; «Откуда» — аккаунт человека; источник и подпись — Mattermost', () => {
    const target = pickPerson(pickPerson(null, person('u1', 'Анна')), person('u2', 'Пётр'));
    expect(targetSource(target)).toBe('mattermost');
    expect(buildChannelNew({ target, accountId: 7, project: '', subject: '' })).toEqual({
      accountId: 7,
      projectId: null,
      to: { kind: 'mm_users', users: ['u1', 'u2'] },
    });
    const accounts = [{ id: 7, source: 'mattermost', title: 'Globex' } as Account];
    expect(fromChoice(target, accounts)).toMatchObject({ preset: 7, fixed: true });
    expect(accountLabel(accounts[0]!)).toBe('Mattermost · Globex');
  });

  it('человек Slack по-прежнему кладётся как Slack (8), не Mattermost', () => {
    const slack: RecipientPerson = {
      type: 'person', account_id: 2, source: 'slack', name: 'С', handle: null,
      to: { kind: 'slack_users', users: ['U1'] }, channel_id: null,
    };
    expect(pickPerson(null, slack)).toMatchObject({ type: 'slack' });
  });

  it('заготовка нового адресата Mattermost подписана именами', () => {
    expect(newRecipientLabel({ kind: 'mm_users', users: ['u1', 'u2'], names: ['Анна', 'Пётр'] })).toBe(
      'Анна, Пётр (новая беседа)',
    );
  });
});

describe('A7: подписи и тексты', () => {
  it('метка источника у людей — «Mattermost» / «MM»; правило раскладки и вложения — Mattermost', () => {
    expect(sourceLabel('mattermost')).toBe('Mattermost');
    expect(sourceBadge('mattermost')).toBe('MM');
    expect(sourceLabel('feed')).toBe('Лента');
    expect(sourceBadge('feed')).toBe('ЛН');
    expect(ruleSourceName('mattermost')).toBe('Mattermost');
    expect(mediaSourceLabel('mattermost')).toBe('Mattermost');
    expect(MEDIA_SOURCES.has('mattermost')).toBe(true);
  });

  it('вложение Mattermost без файла в хабе — кнопка «Загрузить», как у Slack', () => {
    const att = { type: 'document', name: 'a.pdf', size: 10, src_id: 'f1' } as never;
    expect(displayFor(att, null, 'mattermost')).toBe(displayFor(att, null, 'slack'));
  });
});

describe('отметка прочтения беседы, которой уже нет', () => {
  it('404 от ядра (заготовка склеилась с настоящей беседой) — не ошибка; прочие ошибки — наружу', async () => {
    const spy = vi.spyOn(api, 'markChannelRead');
    spy.mockRejectedValueOnce(new api.ApiError(404, 'not found', null));
    await expect(store.markRead(5, 1)).resolves.toBeUndefined();
    spy.mockRejectedValueOnce(new api.ApiError(500, 'boom', null));
    await expect(store.markRead(5, 1)).rejects.toThrow('boom');
  });
});
