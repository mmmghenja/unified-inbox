// Владелец 28.09: после отправки по Ctrl+Enter фокус остаётся в поле ввода и можно сразу набирать следующее —
// поле не отключается на время отправки и очищается сразу; не ушло — отправленное возвращается (перед тем,
// что успел набрать). Беседа (Composer).
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import Composer from '../src/components/Composer.vue';
import { store } from '../src/store/instance';
import { restoreUnsent } from '../src/util/sendField';

const NOW = new Date(2026, 8, 28, 12, 0, 0).getTime();
const SENT = 'первое сообщение';
const NEXT = 'второе';

/** Обещание, которое завершается руками: `ok(value)` или `fail(error)`. */
function deferred<T>() {
  let ok!: (value: T) => void;
  let fail!: (err: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    ok = resolve;
    fail = reject;
  });
  return { promise, ok, fail };
}

interface Case {
  name: string;
  field: string;
  /** Монтирует компонент со шпионом отправки, который ждёт `d`; `failValue` — «не ушло» без исключения. */
  mount: (promise: Promise<unknown>) => Promise<VueWrapper>;
  okValue: unknown;
  failValue: unknown | null;
}

const CASES: Case[] = [
  {
    name: 'Composer (беседа)',
    field: 'textarea',
    async mount(promise) {
      vi.spyOn(store, 'send').mockImplementation((() => promise) as never);
      return mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
    },
    okValue: undefined,
    failValue: null,
  },
];

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

async function ctrlEnter(wrapper: VueWrapper, field: string): Promise<HTMLTextAreaElement> {
  const el = wrapper.get(field).element as HTMLTextAreaElement;
  el.focus();
  await wrapper.get(field).setValue(SENT);
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true }));
  await flushPromises();
  return el;
}

describe('владелец 28.09: после Ctrl+Enter фокус остаётся в поле', () => {
  describe.each(CASES.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    it('пока отправляется: поле не отключено, уже пустое и в фокусе; после — в фокусе', async () => {
      const d = deferred<unknown>();
      const wrapper = await c.mount(d.promise);
      const el = await ctrlEnter(wrapper, c.field);
      expect(el.disabled).toBe(false);
      expect(el.value).toBe('');
      expect(document.activeElement).toBe(el);
      d.ok(c.okValue);
      await flushPromises();
      expect(el.value).toBe('');
      expect(document.activeElement).toBe(el);
      wrapper.unmount();
    });

    it('не ушло: отправленное возвращается перед тем, что успел набрать', async () => {
      const d = deferred<unknown>();
      const wrapper = await c.mount(d.promise);
      const el = await ctrlEnter(wrapper, c.field);
      await wrapper.get(c.field).setValue(NEXT);
      if (c.failValue === null) d.fail(new Error('сеть'));
      else d.ok(c.failValue);
      await flushPromises();
      expect(el.value).toBe(`${SENT}\n${NEXT}`);
      wrapper.unmount();
    });
  });
});

describe('restoreUnsent', () => {
  it('пустое поле — отправленное как было; набранное — после отправленного', () => {
    expect(restoreUnsent('', 'a')).toBe('a');
    expect(restoreUnsent('  ', 'a')).toBe('a');
    expect(restoreUnsent('b', 'a')).toBe('a\nb');
  });
});
