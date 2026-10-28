// Ф80-1 A4: общий механизм всплывающих меню — одно открытое на всё приложение, Esc, нажатие мимо, нажатие по
// своей кнопке не считается «мимо».
import { mount } from '@vue/test-utils';
import { afterEach, describe, expect, it } from 'vitest';
import { defineComponent, nextTick, ref } from 'vue';

import { closeAllPopups, openPopupCount, usePopup } from '../src/util/popup';

const PopupMenu = defineComponent({
  props: { name: { type: String, required: true } },
  setup() {
    const open = ref(false);
    const button = ref<HTMLElement | null>(null);
    const panel = ref<HTMLElement | null>(null);
    usePopup({ open, button: () => button.value, panel: () => panel.value });
    return { open, button, panel };
  },
  template: `<div><button ref="button" class="btn" @click="open = !open">{{ name }}</button><div v-if="open" ref="panel" class="pop"><span class="inner">{{ name }}</span></div></div>`,
});

const Host = defineComponent({
  components: { PopupMenu },
  template: `<div><PopupMenu name="a" /><PopupMenu name="b" /><p class="outside">мимо</p></div>`,
});

const wrappers: Array<{ unmount: () => void }> = [];
function mountHost() {
  const w = mount(Host, { attachTo: document.body });
  wrappers.push(w);
  return w;
}
function pointerDown(el: Element): void {
  el.dispatchEvent(new Event('pointerdown', { bubbles: true }));
}
async function settle(): Promise<void> {
  for (let i = 0; i < 4; i++) await nextTick();
}

afterEach(() => {
  closeAllPopups();
  for (const w of wrappers.splice(0)) w.unmount();
});

describe('usePopup', () => {
  it('второе открытие закрывает первое', async () => {
    const w = mountHost();
    const [a, b] = w.findAll('.btn');
    await a!.trigger('click');
    await settle();
    expect(w.findAll('.pop')).toHaveLength(1);
    await b!.trigger('click');
    await settle();
    expect(w.findAll('.pop')).toHaveLength(1);
    expect(w.find('.inner').text()).toBe('b');
    expect(openPopupCount()).toBe(1);
  });

  it('Esc закрывает', async () => {
    const w = mountHost();
    await w.findAll('.btn')[0]!.trigger('click');
    await settle();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await settle();
    expect(w.findAll('.pop')).toHaveLength(0);
    expect(openPopupCount()).toBe(0);
  });

  it('нажатие мимо закрывает, внутри меню и по своей кнопке — нет', async () => {
    const w = mountHost();
    await w.findAll('.btn')[0]!.trigger('click');
    await settle();
    pointerDown(w.find('.inner').element);
    await settle();
    expect(w.findAll('.pop')).toHaveLength(1);
    pointerDown(w.findAll('.btn')[0]!.element);
    await settle();
    expect(w.findAll('.pop')).toHaveLength(1);
    pointerDown(w.find('.outside').element);
    await settle();
    expect(w.findAll('.pop')).toHaveLength(0);
  });

  it('клик по своей кнопке закрывает меню и не открывает заново', async () => {
    const w = mountHost();
    const a = w.findAll('.btn')[0]!;
    await a.trigger('click');
    await settle();
    pointerDown(a.element);
    await a.trigger('click');
    await settle();
    expect(w.findAll('.pop')).toHaveLength(0);
  });
});
