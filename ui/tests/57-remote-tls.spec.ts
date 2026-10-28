// Ф14-9 (`docs/architecture.md`: панель «Доступ» — https со сроком, http с
// подсказкой, ошибка красным, < 14 дней — предупреждение, адрес не из сертификата, адрес для телефона со схемой
// слушателя; поле ввода — «Из буфера» только в безопасном контексте на сенсорном экране, картинки из
// `navigator.clipboard.read()` уходят обычным вложением, пусто/отказ — «в буфере нет картинки».
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import * as api from '../src/api/client';
import type { Channel, RemoteStatus, UiSession } from '../src/api/types';
import AccessPanel from '../src/components/AccessPanel.vue';
import Composer from '../src/components/Composer.vue';
import { store } from '../src/store/instance';
import { canPasteFromClipboard, clipboardImageName } from '../src/util/clipboard';
import { hostsNotInCert, phoneUrl, TLS_TEXT_HTTP, tlsLine } from '../src/util/remote';

const DAY = 24 * 60 * 60 * 1000;
const NOW = new Date(2026, 8, 28, 12, 0).getTime(); // 28.09.2026, местное время
const UNTIL = new Date(2026, 11, 27, 12, 0).getTime(); // 27.12.2026

const REMOTE: RemoteStatus = {
  enabled: true,
  hosts: ['hub.example.com:8787'],
  password_set: true,
  password_set_ts: 1,
  listening: true,
  failures_24h: 0,
  blocked_until: null,
  tls: { not_after_ms: UNTIL, names: ['hub.example.com'], error: null },
};

const SESSIONS: UiSession[] = [];

async function flushAll(): Promise<void> {
  for (let i = 0; i < 5; i++) await flushPromises();
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.innerHTML = '';
});

// --- T7: строка про https -----------------------------------------------------------------------------------

describe('Ф14-9 T7: tlsLine / hostsNotInCert / phoneUrl', () => {
  it('https со сроком, http с подсказкой, ошибка, < 14 дней, истёк', () => {
    expect(tlsLine(REMOTE, NOW)).toEqual({ text: 'https, сертификат до 27.12', level: 'ok' });
    expect(tlsLine({ ...REMOTE, tls: null }, NOW)).toEqual({ text: TLS_TEXT_HTTP, level: 'warn' });
    expect(TLS_TEXT_HTTP).toContain('без https на телефоне не работают голосовые и вставка картинок');
    const broken = { ...REMOTE, tls: { not_after_ms: null, names: [], error: 'ключ не от этого сертификата' } };
    expect(tlsLine(broken, NOW)).toEqual({
      text: 'https: ошибка — ключ не от этого сертификата; вход с телефона не поднят',
      level: 'error',
    });
    const soon = tlsLine(REMOTE, UNTIL - 13 * DAY - 1000);
    expect(soon.level).toBe('warn');
    expect(soon.text).toContain('осталось 13 дн.');
    expect(tlsLine(REMOTE, UNTIL - 14 * DAY).level).toBe('ok');
    expect(tlsLine(REMOTE, UNTIL + 1).level).toBe('error');
  });

  it('адрес не из сертификата; `*.домен` — одна метка', () => {
    expect(hostsNotInCert(REMOTE)).toEqual([]);
    expect(hostsNotInCert({ ...REMOTE, hosts: ['hub.example.com:8787', 'hub2.example.com:8787'] })).toEqual([
      'hub2.example.com:8787',
    ]);
    const wildcard = { ...REMOTE, tls: { not_after_ms: UNTIL, names: ['*.example.com'], error: null } };
    expect(hostsNotInCert({ ...wildcard, hosts: ['hub.example.com:8787', 'a.b.example.com:8787'] })).toEqual([
      'a.b.example.com:8787',
    ]);
    expect(hostsNotInCert({ ...REMOTE, tls: null })).toEqual([]);
  });

  it('адрес для телефона — со схемой слушателя', () => {
    expect(phoneUrl(REMOTE, 'hub.example.com:8787')).toBe('https://hub.example.com:8787');
    expect(phoneUrl({ ...REMOTE, tls: null }, 'hub.example.com:8787')).toBe('http://hub.example.com:8787');
    const broken = { ...REMOTE, tls: { not_after_ms: null, names: [], error: 'x' } };
    expect(phoneUrl(broken, 'hub.example.com:8787')).toBe('http://hub.example.com:8787');
  });
});

describe('Ф14-9 T7: AccessPanel', () => {
  beforeEach(() => {
    store.state.remoteSession = false;
    vi.useFakeTimers({ now: NOW, toFake: ['Date'] });
  });

  async function mountPanel(remote: RemoteStatus): Promise<VueWrapper> {
    vi.spyOn(api, 'getRemote').mockResolvedValue(remote);
    vi.spyOn(store, 'listSessions').mockResolvedValue(SESSIONS);
    const w = mount(AccessPanel, { attachTo: document.body });
    await flushAll();
    return w;
  }

  it('https: срок, адрес для телефона с https', async () => {
    const w = await mountPanel(REMOTE);
    const line = w.get('.access-tls');
    expect(line.text()).toBe('https, сертификат до 27.12');
    expect(line.classes()).not.toContain('access-error');
    expect(line.classes()).not.toContain('access-warn');
    expect(w.get('.access-phone-url').text()).toBe('С телефона: https://hub.example.com:8787');
    expect(w.find('.access-tls-host').exists()).toBe(false);
    w.unmount();
  });

  it('http: подсказка предупреждением, адрес с http', async () => {
    const w = await mountPanel({ ...REMOTE, tls: null });
    const line = w.get('.access-tls');
    expect(line.text()).toBe(TLS_TEXT_HTTP);
    expect(line.classes()).toContain('access-warn');
    expect(w.get('.access-phone-url').text()).toBe('С телефона: http://hub.example.com:8787');
    w.unmount();
  });

  it('ошибка — красным', async () => {
    const w = await mountPanel({ ...REMOTE, tls: { not_after_ms: null, names: [], error: 'нет файла privkey.pem' } });
    const line = w.get('.access-tls');
    expect(line.text()).toContain('нет файла privkey.pem');
    expect(line.classes()).toContain('access-error');
    w.unmount();
  });

  it('< 14 дней — предупреждение', async () => {
    const w = await mountPanel({ ...REMOTE, tls: { ...REMOTE.tls!, not_after_ms: NOW + 5 * DAY + 1000 } });
    const line = w.get('.access-tls');
    expect(line.classes()).toContain('access-warn');
    expect(line.text()).toContain('осталось 5 дн.');
    w.unmount();
  });

  it('адрес не из сертификата — предупреждение', async () => {
    const w = await mountPanel({ ...REMOTE, hosts: ['hub.example.com:8787', 'hub.other.com:8787'] });
    const warn = w.findAll('.access-tls-host');
    expect(warn.map((p) => p.text())).toEqual(['Сертификат не на этот адрес: hub.other.com:8787']);
    expect(w.get('.access-phone-url').text()).toBe(
      'С телефона: https://hub.example.com:8787, https://hub.other.com:8787',
    );
    w.unmount();
  });
});

// --- П4: «Из буфера» ------------------------------------------------------------------------------------------

function channel(id: number, source = 'telegram'): Channel {
  return {
    id,
    ext_id: `c${id}`,
    kind: 'dm',
    title: 'Канал',
    tier: null,
    muted: false,
    archived: false,
    map_reason: null,
    last_msg_ts: 1000,
    my_msg_count: 0,
    first_seen_ts: null,
    unread: 0,
    project_id: 1,
    source,
  };
}

function setSecure(value: boolean): void {
  Object.defineProperty(window, 'isSecureContext', { value, configurable: true });
}

function setTouch(value: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: value && query === '(pointer: coarse)', media: query }));
  Object.defineProperty(navigator, 'maxTouchPoints', { value: value ? 5 : 0, configurable: true });
}

function setClipboard(read: (() => Promise<unknown[]>) | undefined): void {
  Object.defineProperty(navigator, 'clipboard', {
    value: read === undefined ? undefined : { read },
    configurable: true,
  });
}

function clipboardItem(types: Record<string, Blob>): unknown {
  return { types: Object.keys(types), getType: async (t: string) => types[t]! };
}

describe('Ф14-9 П4: «Из буфера» в поле ввода', () => {
  let uploadSpy: MockInstance;
  let wrapper: VueWrapper | null = null;

  beforeEach(() => {
    uploadSpy = vi.spyOn(store, 'uploadFile').mockImplementation(async (_c: number, _f: Blob, name: string) => ({
      upload_id: `u-${name}`,
      size: 3,
      mime: 'image/png',
      name,
      kind: 'photo',
    }));
  });

  afterEach(() => {
    wrapper?.unmount();
    wrapper = null;
    setSecure(false);
    Reflect.deleteProperty(navigator, 'clipboard');
    Reflect.deleteProperty(navigator, 'maxTouchPoints');
    store.state.sync.channelsById = {};
  });

  /** Ф90-3: «Из буфера» — пункт меню 📎. */
  async function pasteItem(w: VueWrapper): Promise<boolean> {
    if (!w.find('.composer-attach-menu').exists()) {
      const attach = w.find('.composer-attach');
      if (attach.exists()) await attach.trigger('click');
    }
    return w.find('.composer-paste').exists();
  }

  function mountComposer(source = 'telegram'): VueWrapper {
    store.state.sync.channelsById = { 1: channel(1, source) };
    wrapper = mount(Composer, { props: { channelId: 1 }, attachTo: document.body });
    return wrapper;
  }

  it('кнопка — только при https + clipboard.read + сенсорный экран, и только где можно прикрепить', async () => {
    const read = async () => [];
    for (const [secure, touch, clip, expected] of [
      [true, true, true, true],
      [false, true, true, false],
      [true, false, true, false],
      [true, true, false, false],
    ] as const) {
      setSecure(secure);
      setTouch(touch);
      setClipboard(clip ? read : undefined);
      expect(canPasteFromClipboard()).toBe(expected);
      const w = mountComposer();
      expect(await pasteItem(w)).toBe(expected);
      w.unmount();
      wrapper = null;
    }
    setSecure(true);
    setTouch(true);
    setClipboard(read);
    const w = mountComposer('pyrus');
    expect(await pasteItem(w)).toBe(false);
  });

  it('картинки из буфера — вложениями по порядку, текст буфера пропускается', async () => {
    setSecure(true);
    setTouch(true);
    const png = new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' });
    const jpg = new Blob([new Uint8Array([4, 5, 6])], { type: 'image/jpeg' });
    setClipboard(async () => [
      clipboardItem({ 'text/plain': new Blob(['hi']) }),
      clipboardItem({ 'text/html': new Blob(['<img>']), 'image/png': png }),
      clipboardItem({ 'image/jpeg': jpg }),
    ]);
    const w = mountComposer();
    await pasteItem(w);
    await w.get('.composer-paste').trigger('click');
    await flushAll();
    expect(uploadSpy).toHaveBeenCalledTimes(2);
    const [first, second] = uploadSpy.mock.calls;
    expect(first![2]).toBe('image.png');
    expect((first![1] as File).type).toBe('image/png');
    expect(second![2]).toBe('image-2.jpg');
    expect(w.findAll('.composer-file-name').map((n) => n.text())).toEqual(['image.png', 'image-2.jpg']);
    expect(w.find('.composer-error').exists()).toBe(false);
  });

  it('пусто или отказ в разрешении — «в буфере нет картинки», ничего не грузится', async () => {
    setSecure(true);
    setTouch(true);
    setClipboard(async () => [clipboardItem({ 'text/plain': new Blob(['hi']) })]);
    const w = mountComposer();
    await pasteItem(w);
    await w.get('.composer-paste').trigger('click');
    await flushAll();
    expect(w.get('.composer-error').text()).toBe('в буфере нет картинки');
    setClipboard(async () => {
      throw new DOMException('denied', 'NotAllowedError');
    });
    await pasteItem(w);
    await w.get('.composer-paste').trigger('click');
    await flushAll();
    expect(w.get('.composer-error').text()).toBe('в буфере нет картинки');
    expect(uploadSpy).not.toHaveBeenCalled();
  });

  it('имя файла по типу', () => {
    expect(clipboardImageName('image/png', 0)).toBe('image.png');
    expect(clipboardImageName('image/jpeg', 2)).toBe('image-3.jpg');
    expect(clipboardImageName('image/svg+xml', 0)).toBe('image.svgxml');
  });
});
