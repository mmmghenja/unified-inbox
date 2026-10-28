// Ф99-7.3: лента внешней сессии «только смотреть» — события из ядра в форме ленты, без поля ввода; для
// сессии другой машины — подпись «раз в минуту»; не Claude Code — строка.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { SubTranscript } from '../src/api/types';
import SessionTail from '../src/components/SessionTail.vue';

function answer(overrides: Partial<SubTranscript> = {}): SubTranscript {
  return {
    supported: true,
    engine: 'claude',
    remote: false,
    refresh_s: null,
    updated_ms: null,
    events: [
      { id: 1, ts: 1000, kind: 'say', tool: null, summary: 'владелец (next): привет', is_error: false, ref: null },
      { id: 2, ts: 2000, kind: 'text', tool: null, summary: 'готово', is_error: false, ref: null },
    ],
    ...overrides,
  };
}

afterEach(() => vi.restoreAllMocks());

describe('SessionTail', () => {
  it('показывает события стенограммы и не даёт поля ввода', async () => {
    const spy = vi.spyOn(api, 'getSubTranscript').mockResolvedValue(answer());
    const w = mount(SessionTail, { props: { subId: 7 } });
    await flushPromises();
    expect(spy).toHaveBeenCalledWith(7);
    expect(w.text()).toContain('ты написал: привет');
    expect(w.text()).toContain('готово');
    expect(w.find('textarea').exists()).toBe(false);
    expect(w.text()).not.toContain('раз в минуту');
    w.unmount();
  });

  it('сессия другой машины — подпись «обновляется раз в минуту»', async () => {
    vi.spyOn(api, 'getSubTranscript').mockResolvedValue(answer({ remote: true, refresh_s: 60 }));
    const w = mount(SessionTail, { props: { subId: 7 } });
    await flushPromises();
    expect(w.text()).toContain('обновляется раз в минуту');
    w.unmount();
  });

  it('не Claude Code — строка вместо ленты', async () => {
    vi.spyOn(api, 'getSubTranscript').mockResolvedValue(answer({ supported: false, engine: 'opencode', events: [] }));
    const w = mount(SessionTail, { props: { subId: 7 } });
    await flushPromises();
    expect(w.text()).toContain('Лента доступна только для Claude Code');
    expect(w.find('.work-events').exists()).toBe(false);
    w.unmount();
  });
});
