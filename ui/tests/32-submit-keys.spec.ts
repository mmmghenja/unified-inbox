// Ф10-1 A (`docs/architecture.md`): Ctrl+Enter / Cmd+Enter в поле нажимает главную кнопку
// поля ровно как клик; Enter, Shift+Enter, Ctrl+Shift+Enter, Ctrl+Alt+Enter, зажатое (`repeat`) и набор IME
// (`isComposing`) — нет; при недоступной кнопке (пустой текст, идёт отправка) — нет. 
// Монтирование — в документ: jsdom отправляет форму по клику submit-кнопки только у подключённой формы.
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import type { OutboxListItem, SessionRequest } from '../src/api/types';
import Composer from '../src/components/Composer.vue';
import DraftCard from '../src/components/DraftCard.vue';
import OutboxStatusBadge from '../src/components/OutboxStatusBadge.vue';
import WorkRequestCard from '../src/components/WorkRequestCard.vue';
import { store } from '../src/store/instance';
import { isSubmitCombo, onSubmitKey, submitKeyLabel } from '../src/util/submitKeys';

const NOW = new Date(2026, 8, 25, 12, 0, 0).getTime();

function draft(): OutboxListItem {
  return {
    id: 50,
    channel_id: 1,
    project_id: 3,
    channel_title: 'Покупатель',
    text: 'Черновик',
    status: 'pending_approval',
    origin: 'api',
    error: null,
    attempts: 0,
    created_ts: 2000,
    sent_ts: null,
    ext_id: null,
  };
}

function question(): SessionRequest {
  return {
    id: 100,
    session_id: 9,
    project_id: 3,
    kind: 'question',
    tool: 'AskUserQuestion',
    input: { questions: [{ question: 'Какую ветку?', header: 'Ветка', options: [{ label: 'main' }], multiSelect: false }] },
    input_truncated: false,
    reason: null,
    status: 'pending',
    answer: null,
    created_ts: NOW,
    decided_ts: null,
    delivered: false,
    seq: 11,
  };
}

/** Действие, которое не завершается, пока тест не отпустит его (для проверки «идёт отправка»). */
function hanging<T>(value: T): { promise: Promise<T>; release: () => void } {
  let release!: () => void;
  const promise = new Promise<T>((resolve) => (release = () => resolve(value)));
  return { promise, release };
}

interface Case {
  name: string;
  /** Монтирует компонент, возвращает поле и шпион главного действия (`pending` — не завершать). */
  setup: (pending: boolean) => Promise<{ wrapper: VueWrapper; field: string; spy: MockInstance; release: () => void }>;
  /** Проверка аргументов одного вызова главного действия. */
  expectCall: (spy: MockInstance) => void;
}

const TEXT = 'реплика по Ctrl+Enter';

function action<T>(method: keyof typeof store, value: T, pending: boolean) {
  const h = hanging(value);
  const spy = vi
    .spyOn(store, method as never)
    .mockImplementation((() => (pending ? h.promise : Promise.resolve(value))) as never) as unknown as MockInstance;
  return { spy, release: h.release };
}

const CASES: Case[] = [
  {
    name: 'Composer — «Отправить» (submit формы)',
    async setup(pending) {
      const { spy, release } = action('send', undefined, pending);
      const wrapper = mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
      await wrapper.get('textarea').setValue(TEXT);
      return { wrapper, field: 'textarea', spy, release };
    },
    expectCall: (spy) => expect(spy.mock.calls[0]!.slice(0, 2)).toEqual([1, TEXT]),
  },
  {
    name: 'DraftCard (правка) — «Отправить» с изменённым текстом',
    async setup(pending) {
      const { spy, release } = action('approveOutbox', undefined, pending);
      const wrapper = mount(DraftCard, { props: { item: draft() }, attachTo: document.body });
      await wrapper.get('button.draft-edit').trigger('click');
      await wrapper.get('textarea.draft-card-edit').setValue(TEXT);
      return { wrapper, field: 'textarea.draft-card-edit', spy, release };
    },
    expectCall: (spy) => expect(spy.mock.calls[0]).toEqual([50, TEXT]),
  },
  {
    name: 'OutboxStatusBadge — «Отправить» (approve)',
    async setup(pending) {
      const { spy, release } = action('approveOutbox', undefined, pending);
      const wrapper = mount(OutboxStatusBadge, { props: { outboxId: 60, status: 'pending_approval', text: 'было' }, attachTo: document.body });
      await wrapper.get('textarea').setValue(TEXT);
      return { wrapper, field: 'textarea', spy, release };
    },
    expectCall: (spy) => expect(spy.mock.calls[0]).toEqual([60, TEXT]),
  },
  {
    name: 'WorkRequestCard — «свой ответ» -> «Ответить»',
    async setup(pending) {
      const h = hanging('ok' as const);
      const spy = vi.fn(() => (pending ? h.promise : Promise.resolve('ok' as const))) as unknown as MockInstance;
      const release = h.release;
      const wrapper = mount(WorkRequestCard, {
        props: { request: question(), decide: spy as never, retry: async () => 'ok' as const, dismiss: () => undefined },
        attachTo: document.body,
      });
      await wrapper.get('input.work-question-custom').setValue(TEXT);
      return { wrapper, field: 'input.work-question-custom', spy, release };
    },
    expectCall: (spy) =>
      expect(spy.mock.calls[0]![0]).toEqual({ decision: 'allow', answers: { 'Какую ветку?': TEXT } }),
  },
];

const CTRL = { key: 'Enter', ctrlKey: true };
const META = { key: 'Enter', metaKey: true };
const NOT_SUBMIT: Array<[string, KeyboardEventInit]> = [
  ['Enter', { key: 'Enter' }],
  ['Shift+Enter', { key: 'Enter', shiftKey: true }],
  ['Ctrl+Shift+Enter', { key: 'Enter', ctrlKey: true, shiftKey: true }],
  ['Ctrl+Alt+Enter', { key: 'Enter', ctrlKey: true, altKey: true }],
  ['Ctrl+Enter зажатое (repeat)', { key: 'Enter', ctrlKey: true, repeat: true }],
  ['Ctrl+Enter при наборе IME', { key: 'Enter', ctrlKey: true, isComposing: true }],
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Ф10-1 A: Ctrl+Enter нажимает главную кнопку поля', () => {
  describe.each(CASES.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    it.each([
      ['Ctrl+Enter', CTRL],
      ['Cmd+Enter', META],
    ])('%s — главное действие один раз', async (_label, init) => {
      const { wrapper, field, spy } = await c.setup(false);
      await wrapper.get(field).trigger('keydown', init);
      await flushPromises();
      expect(spy).toHaveBeenCalledTimes(1);
      c.expectCall(spy);
      wrapper.unmount();
    });

    it.each(NOT_SUBMIT)('%s — ничего', async (_label, init) => {
      const { wrapper, field, spy } = await c.setup(false);
      await wrapper.get(field).trigger('keydown', init);
      await flushPromises();
      expect(spy).not.toHaveBeenCalled();
      wrapper.unmount();
    });

    it('пустое поле (кнопка недоступна) — ничего', async () => {
      const { wrapper, field, spy } = await c.setup(false);
      await wrapper.get(field).setValue('   ');
      await wrapper.get(field).trigger('keydown', CTRL);
      await flushPromises();
      expect(spy).not.toHaveBeenCalled();
      wrapper.unmount();
    });

    it('идёт отправка (кнопка недоступна) — второй Ctrl+Enter не шлёт', async () => {
      const { wrapper, field, spy, release } = await c.setup(true);
      await wrapper.get(field).trigger('keydown', CTRL);
      await flushPromises();
      await wrapper.get(field).trigger('keydown', CTRL);
      await flushPromises();
      expect(spy).toHaveBeenCalledTimes(1);
      release();
      await flushPromises();
      wrapper.unmount();
    });
  });

  it('у всех полей подсказка мобильной клавиатуре enterkeyhint="enter", у главной кнопки — title', async () => {
    for (const c of CASES) {
      const { wrapper, field } = await c.setup(false);
      const el = wrapper.get(field).element;
      if (el.tagName === 'TEXTAREA') expect(el.getAttribute('enterkeyhint'), c.name).toBe('enter');
      expect(wrapper.find(`button[title="${submitKeyLabel()}"], button[title="Отправить (${submitKeyLabel()})"]`).exists(), c.name).toBe(true);
      wrapper.unmount();
    }
  });
});

describe('Ф10-1 A: util/submitKeys', () => {
  it('preventDefault — только когда сочетание сработало', () => {
    const button = document.createElement('button');
    const click = vi.fn();
    button.addEventListener('click', click);
    const combo = new KeyboardEvent('keydown', { ...CTRL, cancelable: true });
    onSubmitKey(combo, button);
    expect(combo.defaultPrevented).toBe(true);
    expect(click).toHaveBeenCalledTimes(1);

    const plain = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true });
    onSubmitKey(plain, button);
    expect(plain.defaultPrevented).toBe(false);

    button.disabled = true;
    const blocked = new KeyboardEvent('keydown', { ...CTRL, cancelable: true });
    onSubmitKey(blocked, button);
    expect(blocked.defaultPrevented).toBe(false);
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('Ctrl и Cmd вместе — не точное сочетание', () => {
    expect(isSubmitCombo(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, metaKey: true }))).toBe(false);
  });

  it('подпись: Mac — ⌘+Enter, иначе Ctrl+Enter', () => {
    const platform = vi.spyOn(navigator, 'platform', 'get');
    platform.mockReturnValue('MacIntel');
    expect(submitKeyLabel()).toBe('⌘+Enter');
    platform.mockReturnValue('Linux x86_64');
    expect(submitKeyLabel()).toBe('Ctrl+Enter');
  });
});
