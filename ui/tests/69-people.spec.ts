// Ф21-1b (`docs/architecture.md`): люди в интерфейсе.
// Чистые функции `util/people.ts`; клик по имени отправителя и 404 `not_person`/`is_me`; карточка человека
// (переименование, «Отделить» при 2+ контактах, объединение с подтверждением, цифры словами); панель «Люди»
// с предложениями «Похоже, один человек».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Message, PersonBrief, PersonCard as PersonCardData, PersonStats } from '../src/api/types';
import AlbumRow from '../src/components/AlbumRow.vue';
import MessageRow from '../src/components/MessageRow.vue';
import PeoplePanel from '../src/components/PeoplePanel.vue';
import PersonCard from '../src/components/PersonCard.vue';
import { store } from '../src/store/instance';
import { closePerson, openPerson, peopleUi } from '../src/store/people';
import {
  addressText,
  computedLine,
  durationText,
  mergeConfirm,
  percent,
  periodLine,
  reactionsLine,
  replyMeLine,
  replyThemLine,
  statsLines,
  styleLine,
  suggestionLine,
} from '../src/util/people';

function msg(overrides: Partial<Message>): Message {
  return {
    id: 1,
    seq: 1,
    first_seq: 1,
    channel_id: 1,
    project_id: 1,
    ext_id: '1',
    thread_ext_id: null,
    thread_broadcast: false,
    purged: false,
    thread: null,
    reply_to_ext_id: null,
    grouped_id: null,
    is_me: false,
    kind: 'text',
    text: 'привет',
    entities_json: null,
    attachments_json: null,
    forward_json: null,
    mentions_me: false,
    refs_json: null,
    reactions_json: null,
    ts: 1000,
    edited_ts: null,
    deleted: false,
    outbox_id: null,
    outbox_status: null,
    sender: { id: 7, name: 'Иван', handle: null },
    ...overrides,
  };
}

const STATS: PersonStats = {
  in: { total: 30, d30: 7, personal: 30, first_ts: 1, last_ts: new Date(2026, 9, 1, 12).getTime() },
  out: { total: 25, d30: 5, first_ts: 1, last_ts: 5 },
  seen: { total: 40, last_ts: 6 },
  reply_me: { median_ms: 720_000, p75_ms: 3_600_000, n: 18, unanswered: 2 },
  reply_them: { median_ms: 0, p75_ms: 0, n: 0, unanswered: 0 },
  style: {
    chat: { n: 20, median_len: 42, address: 'ты', emoji_share: 0.2, greet_share: 0.1, lang: 'ru' },
    mail: null,
  },
  react: { me_to_them: [{ key: '👍', n: 5 }, { key: '❤️', n: 2 }], them_to_me: [{ key: '🔥', n: 3 }] },
};

function card(overrides: Partial<PersonCardData> = {}): PersonCardData {
  return {
    id: 12,
    name: 'Иван Петров',
    name_manual: false,
    names: ['Иван Петров', 'Ivan P.'],
    known: true,
    contacts: [
      { contact_id: 5, account_id: 1, source: 'telegram', account_title: 'Личный TG', name: 'Иван Петров', handle: '@ivan', email: null, manual: false },
      { contact_id: 6, account_id: 2, source: 'slack', account_title: 'Globex', name: 'Ivan P.', handle: 'ivan.p', email: null, manual: false },
    ],
    projects: [{ id: 1, title: 'Globex' }, { id: 2, title: null }],
    channels: [{ channel_id: 40, title: 'Иван', kind: 'dm', project_id: 1, in: 12, out: 9, seen: 12 }],
    stats: STATS,
    examples: [],
    stats_ts: new Date(2026, 9, 1, 14, 5).getTime(),
    facts: { items: [], count: 0, updated_ts: null },
    ...overrides,
  };
}

function brief(id: number, name: string, source = 'telegram'): PersonBrief {
  return {
    id,
    name,
    known: true,
    sources: [{ source, handle: null, email: null }],
    projects: [],
    last_ts: new Date(2026, 9, 1).getTime(),
    in_30d: 1,
    out_30d: 1,
    reply_me_median_ms: 720_000,
    reply_them_median_ms: null,
  };
}

beforeEach(() => {
  store.state.sync.channelsById = {};
  store.state.sync.messagesByChannel = {};
  closePerson();
  for (const key of Object.keys(peopleUi.notPerson)) delete peopleUi.notPerson[Number(key)];
});

afterEach(() => vi.restoreAllMocks());

describe('util/people.ts', () => {
  it('длительность', () => {
    expect(durationText(30_000)).toBe('< 1 мин');
    expect(durationText(720_000)).toBe('12 мин');
    expect(durationText(2 * 3_600_000)).toBe('2 ч');
    expect(durationText(1.5 * 86_400_000)).toBe('1,5 дн');
  });

  it('обращение, доли, подтверждение', () => {
    expect(addressText('ты')).toBe('на «ты»');
    expect(addressText(null)).toBeNull();
    expect(percent(0.2)).toBe('20 %');
    expect(mergeConfirm('А', 'Б')).toBe('Объединить А и Б?');
    expect(suggestionLine(brief(1, 'Иван', 'telegram'), brief(2, 'Ivan', 'slack'))).toBe(
      'Иван (Telegram) и Ivan (Slack)',
    );
  });

  it('строки цифр словами', () => {
    expect(replyMeLine(STATS)).toBe('Ты отвечаешь: обычно за 12 мин (18 раз), без ответа 2');
    expect(replyThemLine(STATS)).toBeNull();
    expect(periodLine(STATS)).toBe('За 30 дней: 7 от него, 5 от тебя · последний раз 01.10');
    expect(styleLine('chat', STATS.style.chat)).toBe(
      'Как ты пишешь (чаты): на «ты», коротко (~42 знаков), смайлы в 20 %, приветствие в 10 %, по-русски',
    );
    expect(styleLine('mail', null)).toBeNull();
    expect(reactionsLine(STATS)).toBe('Реакции: ты ему 👍 5 · ❤️ 2; он тебе 🔥 3');
    expect(statsLines(STATS)).toHaveLength(4);
    expect(computedLine(new Date(2026, 9, 1, 14, 5).getTime())).toBe('Посчитано в 14:05');
    expect(computedLine(null)).toBe('Цифры появятся в течение часа.');
  });
});

describe('клик по имени отправителя', () => {
  it('имя — кнопка, клик открывает карточку по контакту', async () => {
    const wrapper = mount(MessageRow, { props: { message: msg({}) } });
    const button = wrapper.find('button.sender-name');
    expect(button.text()).toBe('Иван');
    await button.trigger('click');
    expect(peopleUi.card).toEqual({ contactId: 7 });
  });

  it('моё сообщение и сообщение без sender.id — просто текст', () => {
    expect(mount(MessageRow, { props: { message: msg({ is_me: true }) } }).find('button.sender-name').exists()).toBe(false);
    const wrapper = mount(MessageRow, { props: { message: msg({ sender: null }) } });
    expect(wrapper.find('.sender-name').exists()).toBe(false);
  });

  it('альбом: имя тоже кнопка', async () => {
    const wrapper = mount(AlbumRow, { props: { messages: [msg({ id: 1 }), msg({ id: 2 })] } });
    await wrapper.find('button.sender-name').trigger('click');
    expect(peopleUi.card).toEqual({ contactId: 7 });
  });

  it.each(['not_person', 'is_me'])('404 %s — окно закрыто, имя больше не кликается', async (code) => {
    vi.spyOn(api, 'getPersonOfContact').mockRejectedValue(new api.ApiError(404, code, { detail: code }));
    const row = mount(MessageRow, { props: { message: msg({}) } });
    await row.find('button.sender-name').trigger('click');
    const modal = mount(PersonCard, { props: { target: { contactId: 7 } } });
    await flushPromises();
    expect(peopleUi.card).toBeNull();
    expect(peopleUi.notPerson[7]).toBe(true);
    modal.unmount();
    // тот же контакт в другом сообщении — уже не кнопка
    const again = mount(MessageRow, { props: { message: msg({ id: 2 }) } });
    expect(again.find('button.sender-name').exists()).toBe(false);
    expect(again.find('.sender-name').text()).toBe('Иван');
  });
});

describe('PersonCard', () => {
  it('показывает имя, источники, проекты, беседы и цифры', async () => {
    const spy = vi.spyOn(api, 'getPersonOfContact').mockResolvedValue(card());
    const w = mount(PersonCard, { props: { target: { contactId: 5 } } });
    await flushPromises();
    expect(spy).toHaveBeenCalledWith(5);
    expect(w.find('.person-name').text()).toBe('Иван Петров');
    expect(w.findAll('.person-source')).toHaveLength(2);
    expect(w.findAll('.person-split')).toHaveLength(2);
    expect(w.find('.person-projects').text()).toContain('Globex');
    expect(w.find('.person-channel').text()).toBe('Иван');
    expect(w.text()).toContain('Ты отвечаешь: обычно за 12 мин (18 раз), без ответа 2');
    expect(w.text()).toContain('Посчитано в 14:05');
  });

  it('один контакт — «Отделить» нет; нет статистики — подсказка', async () => {
    vi.spyOn(api, 'getPerson').mockResolvedValue(
      card({ contacts: [card().contacts[0]], stats: null, stats_ts: null, channels: [] }),
    );
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    expect(w.find('.person-split').exists()).toBe(false);
    expect(w.find('.person-no-stats').text()).toBe('Цифры появятся в течение часа.');
  });

  it('«Отделить» шлёт контакт и перечитывает карточку', async () => {
    const get = vi.spyOn(api, 'getPerson').mockResolvedValue(card());
    const split = vi.spyOn(api, 'splitPerson').mockResolvedValue(card({ id: 13 }));
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    await w.find('[data-contact-id="6"] .person-split').trigger('click');
    await flushPromises();
    expect(split).toHaveBeenCalledWith([6]);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('переименование и возврат автоматического имени', async () => {
    vi.spyOn(api, 'getPerson').mockResolvedValue(card({ name_manual: true, name: 'Ваня' }));
    const rename = vi.spyOn(api, 'renamePerson').mockResolvedValue(card({ name: 'Иван П.', name_manual: true }));
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    await w.find('.person-pencil').trigger('click');
    await w.find('.person-rename input').setValue('  Иван П.  ');
    await w.find('.person-rename').trigger('submit');
    await flushPromises();
    expect(rename).toHaveBeenCalledWith(12, 'Иван П.');
    expect(w.find('.person-name').text()).toBe('Иван П.');
    await w.find('.person-reset').trigger('click');
    await flushPromises();
    expect(rename).toHaveBeenLastCalledWith(12, null);
  });

  it('Ф24-1c: раздел «Что известно» — факты, метка «вы», пустое состояние, подпись', async () => {
    const facts = {
      items: [
        { id: 3, text: 'Живёт в Берлине', source: 'manual' as const, updated_ts: 2000 },
        { id: 2, text: 'Ведёт закупки', source: 'auto' as const, updated_ts: 1000 },
      ],
      count: 2,
      updated_ts: 2000,
    };
    const get = vi.spyOn(api, 'getPerson').mockResolvedValue(card({ facts }));
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    const rows = w.findAll('.person-fact');
    expect(rows.map((r) => r.find('.person-fact-text').text())).toEqual(['Живёт в Берлине', 'Ведёт закупки']);
    expect(rows[0].find('.person-fact-mine').text()).toBe('вы');
    expect(rows[1].find('.person-fact-mine').exists()).toBe(false);
    expect(rows[0].find('.person-fact-text').attributes('title')).toBeTruthy();
    expect(w.find('.person-facts-caption').text()).toBe('Хаб собирает сам по вашей переписке; правка закрепляет факт');
    expect(w.find('.person-facts-empty').exists()).toBe(false);
    get.mockResolvedValue(card());
    const empty = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    expect(empty.find('.person-facts-empty').text()).toBe('Хаб ещё ничего не записал: факты появятся после переписки');
  });

  it('Ф24-1c: правка, добавление и «убрать» зовут ручки и обновляют карточку; отказ confirm — ничего', async () => {
    const one = (id: number, text: string) => ({ id, text, source: 'auto' as const, updated_ts: 1000 });
    vi.spyOn(api, 'getPerson').mockResolvedValue(
      card({ facts: { items: [one(2, 'Старый факт')], count: 1, updated_ts: 1000 } }),
    );
    const edit = vi.spyOn(api, 'editPersonFact').mockResolvedValue(
      card({ facts: { items: [{ ...one(2, 'Новый текст'), source: 'manual' }], count: 1, updated_ts: 1 } }),
    );
    const add = vi.spyOn(api, 'addPersonFact').mockResolvedValue(
      card({ facts: { items: [one(9, 'Добавлен')], count: 1, updated_ts: 1 } }),
    );
    const remove = vi.spyOn(api, 'removePersonFact').mockResolvedValue(card());
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();

    await w.find('.person-fact-edit').trigger('click');
    await w.find('.person-fact-form input').setValue('  Новый текст ');
    await w.find('.person-fact-form').trigger('submit');
    await flushPromises();
    expect(edit).toHaveBeenCalledWith(12, 2, 'Новый текст');
    expect(w.find('.person-fact-text').text()).toBe('Новый текст');
    expect(w.find('.person-fact-mine').exists()).toBe(true);

    await w.find('.person-fact-add').trigger('click');
    await w.find('.person-fact-new input').setValue('Добавлен');
    await w.find('.person-fact-new').trigger('submit');
    await flushPromises();
    expect(add).toHaveBeenCalledWith(12, 'Добавлен');
    expect(w.find('.person-fact-text').text()).toBe('Добавлен');

    await w.find('.person-fact-remove').trigger('click');
    await flushPromises();
    expect(confirm).toHaveBeenCalledWith('Убрать факт? Хаб не будет добавлять его снова');
    expect(remove).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    await w.find('.person-fact-remove').trigger('click');
    await flushPromises();
    expect(remove).toHaveBeenCalledWith(12, 9);
    expect(w.find('.person-fact').exists()).toBe(false);
    expect(w.find('.person-facts-empty').exists()).toBe(true);
    confirm.mockRestore();
  });

  it('Ф24-1c: 422 ядра при добавлении виден, поле остаётся открытым', async () => {
    vi.spyOn(api, 'getPerson').mockResolvedValue(card());
    vi.spyOn(api, 'addPersonFact').mockRejectedValue(new api.ApiError(422, 'Факт слишком длинный', null));
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    await w.find('.person-fact-add').trigger('click');
    await w.find('.person-fact-new input').setValue('x');
    await w.find('.person-fact-new').trigger('submit');
    await flushPromises();
    expect(w.find('.person-error').text()).toBe('Факт слишком длинный');
    expect(w.find('.person-fact-new').exists()).toBe(true);
  });

  it('«Объединить с…»: поиск, выбор, подтверждение, запрос', async () => {
    vi.spyOn(api, 'getPerson').mockResolvedValue(card());
    const list = vi.spyOn(api, 'listPeople').mockResolvedValue({
      items: [brief(12, 'Иван Петров'), brief(20, 'Ivan Petrov', 'slack')],
      total: 2,
    });
    const merge = vi.spyOn(api, 'mergePeople').mockResolvedValue(card());
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    await w.find('.person-merge-open').trigger('click');
    await w.find('.person-merge input').setValue('ivan');
    await w.find('.person-merge input').trigger('input');
    await flushPromises();
    expect(list).toHaveBeenCalledWith({ q: 'ivan', scope: 'all', limit: 10 });
    const hits = w.findAll('.person-hit');
    expect(hits).toHaveLength(1); // сам человек исключён
    await hits[0].trigger('click');
    expect(w.find('.person-merge-confirm').text()).toBe('Объединить Иван Петров и Ivan Petrov?');
    expect(merge).not.toHaveBeenCalled();
    await w.find('.person-merge-do').trigger('click');
    await flushPromises();
    expect(merge).toHaveBeenCalledWith(12, [20]);
  });

  it('клик по беседе открывает её и закрывает окно', async () => {
    vi.spyOn(api, 'getPerson').mockResolvedValue(card());
    const open = vi.spyOn(store, 'openChannelAt').mockResolvedValue(undefined);
    openPerson(12);
    const w = mount(PersonCard, { props: { target: { personId: 12 } } });
    await flushPromises();
    await w.find('.person-channel').trigger('click');
    expect(open).toHaveBeenCalledWith(1, 40);
    expect(peopleUi.card).toBeNull();
  });
});

describe('PeoplePanel', () => {
  it('список, предложения: «Объединить» и «Нет», клик по строке, подгрузка', async () => {
    const first = Array.from({ length: 50 }, (_, i) => brief(i + 1, `Человек ${i + 1}`));
    const list = vi
      .spyOn(api, 'listPeople')
      .mockResolvedValueOnce({ items: first, total: 51 })
      .mockResolvedValue({ items: [brief(51, 'Человек 51')], total: 51 });
    const pair = { a: brief(1, 'Иван', 'telegram'), b: brief(2, 'Ivan', 'slack'), reason: 'name' as const };
    const second = { a: brief(3, 'Алиса', 'telegram'), b: brief(4, 'Alice', 'slack'), reason: 'name' as const };
    vi.spyOn(api, 'getPeopleSuggestions').mockResolvedValue([pair, second]);
    const merge = vi.spyOn(api, 'mergePeople').mockResolvedValue(card());
    const notSame = vi.spyOn(api, 'notSamePeople').mockResolvedValue(null);

    const w = mount(PeoplePanel);
    await flushPromises();
    expect(list).toHaveBeenCalledWith({ q: '', scope: 'known', limit: 50, offset: 0 });
    expect(w.findAll('.people-row')).toHaveLength(50);
    expect(w.find('.people-suggestions h2').text()).toBe('Похоже, один человек');
    const lines = w.findAll('.people-suggestion-what').map((e) => e.text());
    expect(lines[0]).toBe('Иван (Telegram) и Ivan (Slack)');

    await w.find('.people-more').trigger('click');
    await flushPromises();
    expect(list).toHaveBeenLastCalledWith({ q: '', scope: 'known', limit: 50, offset: 50 });
    expect(w.findAll('.people-row')).toHaveLength(51);

    await w.findAll('.people-suggestion-no')[0].trigger('click');
    await flushPromises();
    expect(notSame).toHaveBeenCalledWith(1, 2);
    expect(w.findAll('.people-suggestion')).toHaveLength(1);

    await w.find('.people-suggestion-merge').trigger('click');
    await flushPromises();
    expect(merge).toHaveBeenCalledWith(3, [4]);

    await w.find('.people-row').trigger('click');
    expect(peopleUi.card).toEqual({ personId: expect.any(Number) });
  });

  it('«Все» перечитывает список с scope=all', async () => {
    const list = vi.spyOn(api, 'listPeople').mockResolvedValue({ items: [], total: 0 });
    vi.spyOn(api, 'getPeopleSuggestions').mockResolvedValue([]);
    const w = mount(PeoplePanel);
    await flushPromises();
    await w.find('.people-scope-all').trigger('click');
    await flushPromises();
    expect(list).toHaveBeenLastCalledWith({ q: '', scope: 'all', limit: 50, offset: 0 });
    expect(w.find('.people-suggestions').exists()).toBe(false);
  });
});
