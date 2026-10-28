// Ф27-1b: util/mentions — метки `@Имя` <-> `<@U…>`, запрос подсказки.
import { describe, expect, it } from 'vitest';

import {
  findMentionQuery,
  insertMention,
  mentionLabel,
  pruneMap,
  showMentions,
  toLabels,
  toWire,
} from '../src/util/mentions';

describe('toLabels / toWire', () => {
  it('кругом: <@U1> -> @Имя -> <@U1>', () => {
    const { text, map } = toLabels('привет <@U111>, глянь', { U111: 'Анна' });
    expect(text).toBe('привет @Анна, глянь');
    expect(toWire(text, map)).toBe('привет <@U111>, глянь');
  });

  it('неизвестный id остаётся как есть', () => {
    expect(toLabels('<@U999> тут', { U111: 'Анна' }).text).toBe('<@U999> тут');
    expect(showMentions('<@U999>', null)).toBe('<@U999>');
  });

  it('повтор метки заменяется везде', () => {
    expect(toWire('@Анна и ещё @Анна', { '@Анна': 'U1' })).toBe('<@U1> и ещё <@U1>');
  });

  it('метка внутри слова не трогается', () => {
    expect(toWire('мой@Анна и @Аннаб и привет@Анна', { '@Анна': 'U1' })).toBe('мой@Анна и @Аннаб и привет@Анна');
    expect(toWire('@Анна.', { '@Анна': 'U1' })).toBe('<@U1>.');
    expect(toWire('(@Анна)', { '@Анна': 'U1' })).toBe('(<@U1>)');
  });

  it('удалённая или правленая метка остаётся текстом', () => {
    expect(toWire('просто Анна', { '@Анна': 'U1' })).toBe('просто Анна');
    expect(toWire('@Ан', { '@Анна': 'U1' })).toBe('@Ан');
  });

  it('имя с пробелом и длинная метка раньше короткой', () => {
    const map = { '@Иван': 'U1', '@Иван Петров': 'U2' };
    expect(toWire('@Иван Петров, @Иван', map)).toBe('<@U2>, <@U1>');
  });

  it('два человека с одним именем — разные метки', () => {
    const { text, map } = toLabels('<@U111> и <@U222>', { U111: 'Анна', U222: 'Анна' });
    expect(text).toBe('@Анна и @Анна·U222');
    expect(toWire(text, map)).toBe('<@U111> и <@U222>');
  });

  it('подставленное не разбирается повторно и спецсимволы метки экранируются', () => {
    expect(toWire('@a.b+c', { '@a.b+c': 'U1' })).toBe('<@U1>');
    expect(toWire('@axb+c', { '@a.b+c': 'U1' })).toBe('@axb+c');
  });

  it('текст без пары — без изменений', () => {
    expect(toWire('<@U5> как есть', {})).toBe('<@U5> как есть');
  });
});

describe('mentionLabel', () => {
  const a = { id: 'U1', name: 'Анна', handle: 'anna' };
  const b = { id: 'U2', name: 'Анна', handle: '@anna2' };
  it('уникальное имя — @Имя', () => {
    expect(mentionLabel(a, [a])).toBe('@Анна');
  });
  it('дубль в подсказке — @Имя·ник', () => {
    expect(mentionLabel(a, [a, b])).toBe('@Анна·anna');
    expect(mentionLabel(b, [a, b])).toBe('@Анна·anna2');
  });
  it('метка уже занята другим id в поле', () => {
    expect(mentionLabel(b, [b], { '@Анна': 'U1' })).toBe('@Анна·anna2');
    expect(mentionLabel(a, [a], { '@Анна': 'U1' })).toBe('@Анна');
  });
});

describe('pruneMap', () => {
  it('убирает метки, которых нет в тексте', () => {
    expect(pruneMap('@Анна', { '@Анна': 'U1', '@Петя': 'U2' })).toEqual({ '@Анна': 'U1' });
  });
});

describe('findMentionQuery / insertMention', () => {
  it('@ в начале и после пробела', () => {
    expect(findMentionQuery('@ан', 3)).toEqual({ start: 0, query: 'ан' });
    expect(findMentionQuery('привет @Ан', 10)).toEqual({ start: 7, query: 'Ан' });
    expect(findMentionQuery('a\n@bo', 5)).toEqual({ start: 2, query: 'bo' });
  });
  it('нет запроса: пустой, внутри слова, длиннее 30, каретка раньше', () => {
    expect(findMentionQuery('привет @', 8)).toBeNull();
    expect(findMentionQuery('почта a@b', 9)).toBeNull();
    expect(findMentionQuery(`@${'я'.repeat(31)}`, 32)).toBeNull();
    expect(findMentionQuery('@ан привет', 1)).toBeNull();
    expect(findMentionQuery('@ан привет', 3)).toEqual({ start: 0, query: 'ан' });
  });
  it('вставка заменяет @запрос на метку и пробел', () => {
    expect(insertMention('привет @ан тут', 7, 10, '@Анна')).toEqual({ text: 'привет @Анна тут', caret: 13 });
    expect(insertMention('@ан', 0, 3, '@Анна')).toEqual({ text: '@Анна ', caret: 6 });
  });
});
