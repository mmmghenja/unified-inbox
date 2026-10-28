// Ф23-2: «кто поставил реакцию» — разбор `by`/`by_more`, всплывашка-список при наведении, долгое нажатие на телефоне.
import { mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import ReactionsLine from '../src/components/ReactionsLine.vue';
import { parseReactions } from '../src/render/reactions';

describe('Ф23-2: parseReactions — by / by_more', () => {
  it('имена и остаток разбираются; старые строки без полей — пустые', () => {
    const [a, b, c] = parseReactions(
      JSON.stringify([
        { emoji: '👍', count: 4, me: true, by: ['Пётр', 'Анна'], by_more: 1 },
        { emoji: '❤️', count: 1 },
        { custom: '55', count: 2, by: ['Игорь'] },
      ]),
    );
    expect(a).toMatchObject({ by: ['Пётр', 'Анна'], byMore: 1, me: true });
    expect(b!.by).toBeUndefined();
    expect(b!.byMore).toBeUndefined();
    expect(c).toMatchObject({ custom: true, by: ['Игорь'] });
  });

  it('мусор в by/by_more отбрасывается, список режется до 100', () => {
    const many = Array.from({ length: 150 }, (_, i) => `n${i}`);
    const [a, b] = parseReactions(
      JSON.stringify([
        { emoji: '👍', count: 1, by: ['ok', 5, '', null, { x: 1 }], by_more: -3 },
        { emoji: '🔥', count: 150, by: many, by_more: 'x' },
      ]),
    );
    expect(a!.by).toEqual(['ok']);
    expect(a!.byMore).toBeUndefined();
    expect(b!.by).toHaveLength(100);
    expect(b!.byMore).toBeUndefined();
  });
});

function chips(): InstanceType<typeof ReactionsLine>['$props']['reactions'] {
  return parseReactions(
    JSON.stringify([
      { emoji: '👍', count: 4, me: true, by: ['Пётр', 'Анна'], by_more: 1 },
      { emoji: '🔥', count: 1, me: false },
    ]),
  );
}

describe('Ф23-2: всплывашка со списком', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('наведение — список: «ты» первой, затем имена по порядку, «и ещё N без имени»; уход — убрана', async () => {
    const wrapper = mount(ReactionsLine, { props: { reactions: chips(), clickable: true } });
    expect(wrapper.find('.reaction-who').exists()).toBe(false);
    await wrapper.findAll('.reaction-wrap')[0]!.trigger('pointerenter', { pointerType: 'mouse' });
    const lines = wrapper.findAll('.reaction-who li').map((li) => li.text());
    expect(lines).toEqual(['ты', 'Пётр', 'Анна', 'и ещё 1 без имени']);
    await wrapper.findAll('.reaction-wrap')[0]!.trigger('pointerleave', { pointerType: 'mouse' });
    expect(wrapper.find('.reaction-who').exists()).toBe(false);
  });

  it('чип без данных о поставивших — всплывашки нет', async () => {
    const wrapper = mount(ReactionsLine, { props: { reactions: chips(), clickable: true } });
    await wrapper.findAll('.reaction-wrap')[1]!.trigger('pointerenter', { pointerType: 'mouse' });
    expect(wrapper.find('.reaction-who').exists()).toBe(false);
  });

  it('каждый чип — свой список', async () => {
    const wrapper = mount(ReactionsLine, { props: { reactions: chips(), clickable: true } });
    await wrapper.findAll('.reaction-wrap')[0]!.trigger('pointerenter', { pointerType: 'mouse' });
    expect(wrapper.findAll('.reaction-who')).toHaveLength(1);
  });

  it('телефон: касание не открывает всплывашку «как наведение»', async () => {
    const wrapper = mount(ReactionsLine, { props: { reactions: chips(), clickable: true } });
    await wrapper.findAll('.reaction-wrap')[0]!.trigger('pointerenter', { pointerType: 'touch' });
    expect(wrapper.find('.reaction-who').exists()).toBe(false);
  });

  it('клик по чипу по-прежнему ставит/снимает', async () => {
    const wrapper = mount(ReactionsLine, { props: { reactions: chips(), clickable: true } });
    await wrapper.findAll('button.reaction')[0]!.trigger('click');
    expect(wrapper.emitted('toggle')).toEqual([['👍']]);
  });

  it('телефон: долгое нажатие (500 мс) — всплывашка, клик после неё реакцию не трогает; короткое — как раньше', async () => {
    const wrapper = mount(ReactionsLine, { props: { reactions: chips(), clickable: true } });
    const button = wrapper.findAll('button.reaction')[0]!;
    await button.trigger('pointerdown', { pointerType: 'touch' });
    await vi.advanceTimersByTimeAsync(499);
    expect(wrapper.find('.reaction-who').exists()).toBe(false);
    await vi.advanceTimersByTimeAsync(2);
    expect(wrapper.find('.reaction-who').exists()).toBe(true);
    await button.trigger('pointerup', { pointerType: 'touch' });
    await button.trigger('click');
    expect(wrapper.emitted('toggle')).toBeUndefined();

    // короткое нажатие: до 500 мс отпустили — таймер снят, клик ставит реакцию
    await wrapper.findAll('.reaction-wrap')[0]!.trigger('pointerleave', { pointerType: 'mouse' });
    await button.trigger('pointerdown', { pointerType: 'touch' });
    await vi.advanceTimersByTimeAsync(200);
    await button.trigger('pointerup', { pointerType: 'touch' });
    await vi.advanceTimersByTimeAsync(600);
    await button.trigger('click');
    expect(wrapper.emitted('toggle')).toEqual([['👍']]);
  });
});
