// Ф56-5б (`docs/architecture.md` и 6): терминал в интерфейсе. xterm подменён заглушкой (jsdom
// не рисует) — проверяется склейка: один поток на вкладку и его закрытие на скрытой вкладке (К2), «▶ в терминал» только
// у разрешённого текста модели (К3), восстановление режимов из снимка (К6), `term_busy` -> вопрос -> `force` (К8),
// подавление ответов xterm (К9), размер шлёт только видимая вкладка с фокусом (К10), один `input` в полёте (К11),
// автораскрытие без кражи фокуса (К14), поле «строка» на телефоне, кнопки открытия.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { TermInfo, TermSnapshotFrame } from '../src/api/types';
import AgentQueue from '../src/components/AgentQueue.vue';
import MessageText from '../src/components/MessageText';
import ModelText from '../src/components/ModelText.vue';
import SessionLiveLine from '../src/components/SessionLiveLine.vue';
import TerminalHost from '../src/components/TerminalHost.vue';
import TopBar from '../src/components/TopBar.vue';
import TopMenu from '../src/components/TopMenu.vue';
import { renderModelMarkdown } from '../src/render/markdown';
import { store } from '../src/store/instance';
import {
  canReportSize,
  openTerminal,
  registerSink,
  reportSize,
  resetTerminal,
  runInTerminal,
  sendTermInput,
  setTermSourceFactory,
  termState,
  type TermSourceLike,
} from '../src/store/terminal';
import { bytesToB64, textToBytes } from '../src/util/base64';
import { layout } from '../src/util/layout';
import { restoreSequences } from '../src/util/xtermGlue';

// --- заглушка xterm ---------------------------------------------------------------------------------------------

const mocks = vi.hoisted(() => {
  interface Handler {
    id: { prefix?: string; intermediates?: string; final?: string };
    cb: (...a: unknown[]) => boolean;
  }
  class MockTerminal {
    static instances: MockTerminal[] = [];
    cols = 80;
    rows = 24;
    log: string[] = [];
    writes: (string | Uint8Array)[] = [];
    csi: Handler[] = [];
    dcs: Handler[] = [];
    osc = new Map<number, (data: string) => boolean>();
    focused = 0;
    disposed = false;
    textarea = document.createElement('textarea');
    dataCb: ((d: string) => void) | null = null;
    parser = {
      registerCsiHandler: (id: Handler['id'], cb: Handler['cb']) => {
        this.csi.push({ id, cb });
        return { dispose() {} };
      },
      registerDcsHandler: (id: Handler['id'], cb: Handler['cb']) => {
        this.dcs.push({ id, cb });
        return { dispose() {} };
      },
      registerOscHandler: (ident: number, cb: (data: string) => boolean) => {
        this.osc.set(ident, cb);
        return { dispose() {} };
      },
    };
    constructor(public options: Record<string, unknown>) {
      MockTerminal.instances.push(this);
    }
    loadAddon(): void {}
    open(el: HTMLElement): void {
      this.textarea.className = 'xterm-helper-textarea';
      el.appendChild(this.textarea);
    }
    onData(cb: (d: string) => void) {
      this.dataCb = cb;
      return { dispose() {} };
    }
    onBinary() {
      return { dispose() {} };
    }
    attachCustomKeyEventHandler(): void {}
    hasSelection(): boolean {
      return false;
    }
    getSelection(): string {
      return '';
    }
    write(d: string | Uint8Array): void {
      this.writes.push(d);
    }
    reset(): void {
      this.log.push('reset');
    }
    resize(c: number, r: number): void {
      this.cols = c;
      this.rows = r;
      this.log.push(`resize ${c}x${r}`);
    }
    scrollToBottom(): void {
      this.log.push('scrollToBottom');
    }
    focus(): void {
      this.focused += 1;
    }
    dispose(): void {
      this.disposed = true;
    }
  }
  const fitState = { undefinedFirst: 0, calls: 0 };
  return { MockTerminal, fitState };
});

vi.mock('@xterm/xterm', () => ({ Terminal: mocks.MockTerminal }));
vi.mock('@xterm/xterm/css/xterm.css', () => ({}));
vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    proposeDimensions() {
      mocks.fitState.calls += 1;
      if (mocks.fitState.calls <= mocks.fitState.undefinedFirst) return undefined;
      return { cols: 100, rows: 30 };
    }
  },
}));

// --- заглушки сети ---------------------------------------------------------------------------------------------

class FakeSource implements TermSourceLike {
  static all: FakeSource[] = [];
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  closed = false;
  constructor(public url: string) {
    FakeSource.all.push(this);
  }
  addEventListener(type: string, cb: (ev: MessageEvent) => void): void {
    (this.listeners[type] ??= []).push(cb);
  }
  close(): void {
    this.closed = true;
  }
  emit(type: string, data: unknown): void {
    for (const cb of this.listeners[type] ?? []) cb({ data: JSON.stringify(data) } as MessageEvent);
  }
}

interface Sent {
  method: string;
  path: string;
  body: Record<string, unknown> | undefined;
}
let sent: Sent[] = [];
let terms: TermInfo[] = [];
let runStatus: { status: number; detail: string } | null = null;
let streamStatus = 200;
let inputGate: Promise<void> | null = null;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function info(id: number, extra: Partial<TermInfo> = {}): TermInfo {
  return { id, title: 'bash в ~', cols: 80, rows: 24, created_ts: 1, alive: true, command: 'bash', ...extra };
}

function posts(suffix: string): Sent[] {
  return sent.filter((s) => s.method === 'POST' && s.path.endsWith(suffix));
}

function snapshot(id: number, pos: string, extra: Partial<TermSnapshotFrame> = {}): TermSnapshotFrame {
  return {
    term_id: id,
    pos,
    cols: 100,
    rows: 30,
    data_b64: bytesToB64(textToBytes('привет\r\nмир')),
    cursor: [3, 1],
    alt_screen: false,
    modes: { cursor_flag: true },
    ...extra,
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
function visible(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
  api.setCsrf('c');
  sent = [];
  terms = [];
  runStatus = null;
  streamStatus = 200;
  inputGate = null;
  FakeSource.all = [];
  mocks.MockTerminal.instances = [];
  mocks.fitState.undefinedFirst = 0;
  mocks.fitState.calls = 0;
  layout.mode = 'full';
  visible('visible');
  vi.spyOn(document, 'hasFocus').mockReturnValue(true);
  setTermSourceFactory((url) => new FakeSource(url));
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, 'http://localhost');
      const s: Sent = { method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined };
      sent.push(s);
      const p = s.path;
      if (p === '/api/v1/term' && s.method === 'GET') return json(terms);
      if (p === '/api/v1/term' && s.method === 'POST') {
        const created = info(terms.length === 0 ? 1 : Math.max(...terms.map((t) => t.id)) + 1);
        terms = [...terms, created];
        return json(created);
      }
      if (p.endsWith('/run')) {
        if (runStatus !== null) return json({ detail: runStatus.detail }, runStatus.status);
        return json({ run_id: 7, term_id: Number(p.split('/')[4]) });
      }
      if (p.endsWith('/input')) {
        if (inputGate !== null) await inputGate;
        return json({ ok: true });
      }
      if (p === '/api/v1/term/stream') return json({ detail: 'x' }, streamStatus);
      if (p.endsWith('/resize')) return json({ cols: 1, rows: 1 });
      if (p === '/api/v1/term/runs') return json([{ id: 1, ts: 1, term_id: 1, who: 'ui', source_kind: 'ui_block', source_ref: null, text: 'ls' }]);
      if (s.method === 'DELETE') return json({ id: 1, closed: true });
      return json([]);
    }),
  );
});

afterEach(() => {
  resetTerminal();
  setTermSourceFactory(null);
  layout.mode = 'full';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = '';
});

async function mountHost() {
  const w = mount(TerminalHost, { attachTo: document.body });
  await flushPromises();
  return w;
}

/** Панель терминала подгружается лениво (async-компонент) — ждём, пока появится заглушка xterm. */
async function paneReady(): Promise<InstanceType<typeof mocks.MockTerminal>> {
  for (let i = 0; i < 50 && mocks.MockTerminal.instances.length === 0; i += 1) {
    await sleep(20);
    await flushPromises();
  }
  return mocks.MockTerminal.instances[0]!;
}

const FENCE = '```bash\nls -la\n```';

describe('56-5б К3: «▶ в терминал» только у разрешённого текста модели', () => {
  it('renderModelMarkdown: кнопка есть только при runnable, «копировать» — всегда', () => {
    expect(renderModelMarkdown(FENCE)).toContain('md-copy');
    expect(renderModelMarkdown(FENCE)).not.toContain('md-run');
    const html = renderModelMarkdown(FENCE, { runnable: true });
    expect(html).toContain('class="md-run"');
    expect(html).toContain('class="md-copy"');
  });

  it('текст модели не может сам принести кнопку (сырой HTML — текстом)', () => {
    const html = renderModelMarkdown('<button class="md-run">x</button>', { runnable: false });
    expect(html).not.toContain('<button');
  });

  it('ModelText: без runnable кнопки нет; с runnable — клик вставляет ровно текст блока', async () => {
    const plain = mount(ModelText, { props: { text: FENCE }, attachTo: document.body });
    expect(plain.find('.md-run').exists()).toBe(false);
    plain.unmount();

    const w = mount(ModelText, { props: { text: FENCE, runnable: true }, attachTo: document.body });
    expect(w.find('.md-run').text()).toBe('▶ в терминал');
    await w.find('.md-run').trigger('click');
    await flushPromises();
    // Терминалов нет — сначала создаётся, потом вставка именно текста блока.
    expect(posts('/term')).toHaveLength(1);
    const run = posts('/run')[0]!;
    expect(run.path).toBe('/api/v1/term/1/run');
    expect(run.body).toEqual({ text: 'ls -la\n', source: { kind: 'ui_block' } });
    expect(termState.open).toBe(true);
    w.unmount();
  });

  it('у MessageText, SessionLiveLine и DraftCard кнопки нет', () => {
    const msg = mount(MessageText, { props: { text: FENCE, markdown: true } });
    expect(msg.find('.md-code').exists()).toBe(true);
    expect(msg.find('.md-run').exists()).toBe(false);
    const live = mount(SessionLiveLine, { props: { live: { session_id: 1, block: 'b', kind: 'text', text: FENCE, cut: false } as never } });
    expect(live.find('.md-code').exists()).toBe(true);
    expect(live.find('.md-run').exists()).toBe(false);
    const draft = readFileSync(resolve(__dirname, '../src/components/DraftCard.vue'), 'utf8');
    expect(draft).not.toMatch(/runnable/);
  });

  it('runnable включён у ленты сессии, полного текста карточки, ответов агента', () => {
    for (const file of ['WorkEventFeed', 'OwnerFullBody', 'OwnerTalk']) {
      const src = readFileSync(resolve(__dirname, `../src/components/${file}.vue`), 'utf8');
      expect(src, file).toMatch(/\brunnable\b/);
    }
  });
});

describe('56-5б К2: один поток на вкладку', () => {
  it('открытие панели — один поток; скрытая вкладка закрывает его, показ — открывает с вектором позиций', async () => {
    terms = [info(1), info(2)];
    await openTerminal();
    await flushPromises();
    expect(FakeSource.all).toHaveLength(1);
    expect(FakeSource.all[0]!.url).toBe('/api/v1/term/stream');
    const s = FakeSource.all[0]!;
    s.emit('snapshot', snapshot(1, 'aa:10'));
    s.emit('snapshot', snapshot(2, 'aa:0'));
    s.emit('out', { term_id: 1, pos: 'aa:25', data_b64: bytesToB64(textToBytes('x')) });

    visible('hidden');
    expect(s.closed).toBe(true);
    expect(FakeSource.all).toHaveLength(1);

    visible('visible');
    expect(FakeSource.all).toHaveLength(2);
    expect(decodeURIComponent(FakeSource.all[1]!.url)).toBe('/api/v1/term/stream?pos=1@aa:25,2@aa:0');
  });

  it('обрыв — повторное подключение с теми же позициями; закрытие панели закрывает поток', async () => {
    terms = [info(1)];
    await openTerminal();
    const s = FakeSource.all[0]!;
    s.emit('snapshot', snapshot(1, 'aa:5'));
    s.emit('error', {});
    expect(s.closed).toBe(true);
    await sleep(1700);
    expect(FakeSource.all).toHaveLength(2);
    expect(decodeURIComponent(FakeSource.all[1]!.url)).toContain('pos=1@aa:5');
    const { closePanel } = await import('../src/store/terminal');
    closePanel();
    expect(FakeSource.all[1]!.closed).toBe(true);
  });

  it('кадры раньше панели терминала ждут в очереди и отдаются при регистрации', async () => {
    terms = [info(1)];
    await openTerminal();
    const s = FakeSource.all[0]!;
    s.emit('snapshot', snapshot(1, 'aa:5'));
    s.emit('out', { term_id: 1, pos: 'aa:6', data_b64: bytesToB64(textToBytes('z')) });
    const got: string[] = [];
    registerSink(1, {
      snapshot: (f) => got.push(`snap ${f.pos}`),
      out: (b) => got.push(`out ${new TextDecoder().decode(b)}`),
      size: () => undefined,
      closed: () => undefined,
    });
    expect(got).toEqual(['snap aa:5', 'out z']);
  });
});

describe('56-5б К6: восстановление режимов', () => {
  it('restoreSequences: alt-screen, область, origin (строка от верха области), мышь, клавиши', () => {
    const r = restoreSequences(
      snapshot(1, 'a:1', {
        alt_screen: true,
        cursor: [4, 7],
        modes: {
          cursor_flag: false,
          keypad_cursor_flag: true,
          keypad_flag: true,
          origin_flag: true,
          scroll_region: [5, 20],
          mouse_any_flag: true,
          mouse_sgr_flag: true,
          wrap_flag: false,
          insert_flag: true,
        },
      }),
    );
    expect(r.head).toBe('\x1b[?1049h');
    expect(r.tail).toBe('\x1b[6;21r\x1b[?6h\x1b[3;5H\x1b[?1h\x1b=\x1b[?25l\x1b[4h\x1b[?7l\x1b[?1003h\x1b[?1006h');
  });

  it('обычный экран: курсор абсолютный, область на весь экран не задаётся', () => {
    const r = restoreSequences(snapshot(1, 'a:1', { cursor: [3, 1], modes: { cursor_flag: true, scroll_region: [0, 29] } }));
    expect(r.head).toBe('');
    expect(r.tail).toBe('\x1b[2;4H');
  });

  it('панель: снимок -> reset, размер сервера, данные, режимы; затем вывод байтами', async () => {
    terms = [info(1)];
    layout.mode = 'full';
    const host = await mountHost();
    await openTerminal();
    const term = await paneReady();
    const s = FakeSource.all[0]!;
    s.emit('snapshot', snapshot(1, 'aa:5', { alt_screen: true, modes: { cursor_flag: false, mouse_sgr_flag: true } }));
    expect(term.log.slice(0, 2)).toEqual(['reset', 'resize 100x30']);
    expect(term.writes[0]).toBe('\x1b[?1049h');
    expect(new TextDecoder().decode(term.writes[1] as Uint8Array)).toBe('привет\r\nмир');
    expect(term.writes[2]).toBe('\x1b[2;4H\x1b[?25l\x1b[?1006h');
    s.emit('out', { term_id: 1, pos: 'aa:9', data_b64: bytesToB64(textToBytes('ж')) });
    expect(term.writes.at(-1)).toBeInstanceOf(Uint8Array);
    expect(new TextDecoder().decode(term.writes.at(-1) as Uint8Array)).toBe('ж');
    host.unmount();
  });
});

describe('56-5б К9: ответы xterm на запросы программы подавлены', () => {
  it('DA/DSR/DECRQM/XTVERSION/OSC-запросы — «обработаны», установка цвета — нет', async () => {
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    const term = await paneReady();
    const finals = term.csi.map((h) => `${h.id.prefix ?? ''}${h.id.intermediates ?? ''}${h.id.final}`);
    expect(finals).toEqual(expect.arrayContaining(['c', '>c', '=c', 'n', '?n', '$p', '?$p', 't', '>q']));
    expect(term.csi.every((h) => h.cb() === true)).toBe(true);
    expect(term.dcs[0]!.id).toEqual({ intermediates: '$', final: 'q' });
    for (const ident of [4, 10, 11, 12]) expect(term.osc.has(ident)).toBe(true);
    expect(term.osc.get(11)!('?')).toBe(true);
    expect(term.osc.get(4)!('1;?')).toBe(true);
    expect(term.osc.get(11)!('rgb:11/22/33')).toBe(false);
    host.unmount();
  });
});

describe('56-5б К11: порядок ввода', () => {
  it('в полёте один input; накопленное склеивается в следующий', async () => {
    let release: () => void = () => undefined;
    inputGate = new Promise<void>((r) => (release = r));
    sendTermInput(1, textToBytes('a'));
    await sleep(30);
    expect(posts('/input')).toHaveLength(1);
    sendTermInput(1, textToBytes('b'));
    await sleep(30);
    sendTermInput(1, textToBytes('c'));
    await sleep(30);
    expect(posts('/input')).toHaveLength(1); // первый ещё не вернулся
    inputGate = null;
    release();
    await sleep(40);
    const all = posts('/input');
    expect(all).toHaveLength(2);
    expect(atob(all[0]!.body!.data_b64 as string)).toBe('a');
    expect(atob(all[1]!.body!.data_b64 as string)).toBe('bc');
  });

  it('быстрый набор склеивается за 10 мс в один запрос', async () => {
    sendTermInput(2, textToBytes('l'));
    sendTermInput(2, textToBytes('s'));
    await sleep(40);
    expect(posts('/input')).toHaveLength(1);
    expect(atob(posts('/input')[0]!.body!.data_b64 as string)).toBe('ls');
  });
});

describe('Ф56-5 телефон: поток и размер', () => {
  it('пока поток не открыт — «подключаюсь…»; открылся — строка уходит', async () => {
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    await flushPromises();
    const note = () => document.body.querySelector('[data-test="term-stream-note"]');
    expect(note()!.textContent).toContain('подключаюсь');
    FakeSource.all[0]!.emit('open', {});
    await flushPromises();
    expect(note()).toBeNull();
    host.unmount();
  });

  it('обрыв потока: «нет связи… повтор через N с» и причина по статусу (429)', async () => {
    terms = [info(1)];
    streamStatus = 429;
    const host = await mountHost();
    await openTerminal();
    FakeSource.all[0]!.emit('open', {});
    FakeSource.all[0]!.emit('error', {});
    await flushPromises();
    await sleep(20);
    await flushPromises();
    const text = document.body.querySelector('[data-test="term-stream-note"]')!.textContent!;
    expect(text).toContain('закрой лишние вкладки хаба');
    host.unmount();
  });

  it('обрыв без статуса ошибки: строка «нет связи с терминалом (повтор через 2 с)»', async () => {
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    FakeSource.all[0]!.emit('error', {});
    await flushPromises();
    expect(document.body.querySelector('[data-test="term-stream-note"]')!.textContent).toMatch(/нет связи с терминалом \(повтор через 2 с\)/);
    host.unmount();
  });

  it('canReportSize: на телефоне хватает видимости, на ноутбуке нужен фокус', () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(false);
    layout.mode = 'phone';
    expect(canReportSize()).toBe(true);
    visible('hidden');
    expect(canReportSize()).toBe(false);
    visible('visible');
    layout.mode = 'full';
    expect(canReportSize()).toBe(false);
  });
});

describe('56-5б К10: размер шлёт только видимая вкладка с фокусом', () => {
  it('без фокуса или на скрытой вкладке — не шлёт; повтор того же размера — не шлёт', () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(false);
    reportSize(1, 100, 30);
    expect(posts('/resize')).toHaveLength(0);
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    visible('hidden');
    reportSize(1, 100, 30);
    expect(posts('/resize')).toHaveLength(0);
    visible('visible');
    reportSize(1, 100, 30);
    reportSize(1, 100, 30);
    expect(posts('/resize')).toHaveLength(1);
    expect(posts('/resize')[0]!.body).toEqual({ cols: 100, rows: 30 });
  });

  it('кадр size подгоняет остальные вкладки без отправки на сервер', async () => {
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    const term = await paneReady();
    FakeSource.all[0]!.emit('size', { term_id: 1, cols: 120, rows: 40 });
    expect(term.log).toContain('resize 120x40');
    expect(posts('/resize').some((p) => p.body?.cols === 120)).toBe(false);
    host.unmount();
  });
});

describe('56-5б К8: терминал занят', () => {
  it('409 term_busy -> вопрос в панели -> повтор с force', async () => {
    terms = [info(1)];
    const host = await mountHost();
    runStatus = { status: 409, detail: 'term_busy' };
    const res = await runInTerminal('rm -rf x', { kind: 'ui_block' });
    await flushPromises();
    expect(res).toEqual({ ok: false, busy: true });
    expect(document.body.querySelector('[data-test="term-busy-ask"]')!.textContent).toContain('Терминал занят — вставить всё равно?');
    runStatus = null;
    (document.body.querySelector('[data-test="term-busy-yes"]') as HTMLButtonElement).click();
    await flushPromises();
    const runs = posts('/run');
    expect(runs.at(-1)!.body).toEqual({ text: 'rm -rf x', source: { kind: 'ui_block' }, force: true });
    expect(runs[0]!.body).not.toHaveProperty('force');
    expect(document.body.querySelector('[data-test="term-busy-ask"]')).toBeNull();
    host.unmount();
  });

  it('422 control_chars — понятное сообщение, терминал не трогается', async () => {
    terms = [info(1)];
    const host = await mountHost();
    runStatus = { status: 422, detail: 'control_chars' };
    await runInTerminal('x\x1b[2J', { kind: 'ui_block' });
    await flushPromises();
    expect(termState.error).toContain('управляющие символы');
    host.unmount();
  });
});

describe('56-5б К14: автораскрытие на пароль', () => {
  async function collapsedWith(): Promise<FakeSource> {
    terms = [info(1)];
    await openTerminal();
    await flushPromises();
    termState.collapsed = true;
    return FakeSource.all[0]!;
  }

  it('подсказка sudo в свёрнутой панели раскрывает её и просит НЕ принудительный фокус', async () => {
    const s = await collapsedWith();
    const before = termState.focusReq.n;
    s.emit('out', { term_id: 1, pos: 'a:1', data_b64: bytesToB64(textToBytes('[sudo] password for evg: ')) });
    expect(termState.collapsed).toBe(false);
    expect(termState.focusReq.n).toBe(before + 1);
    expect(termState.focusReq.force).toBe(false);
  });

  it('подсказка, разрезанная между кадрами, тоже находится; обычный вывод — нет', async () => {
    const s = await collapsedWith();
    s.emit('out', { term_id: 1, pos: 'a:1', data_b64: bytesToB64(textToBytes('hello\r\n')) });
    expect(termState.collapsed).toBe(true);
    s.emit('out', { term_id: 1, pos: 'a:2', data_b64: bytesToB64(textToBytes('Passw')) });
    expect(termState.collapsed).toBe(true);
    s.emit('out', { term_id: 1, pos: 'a:3', data_b64: bytesToB64(textToBytes('ord: ')) });
    // `^Password:` — только в начале строки: «Passw»+«ord:» после «hello\r\n» — начало строки
    expect(termState.collapsed).toBe(false);
  });

  it('фокус в поле ввода не отбирается; по клику владельца — отбирается', async () => {
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    const term = await paneReady();
    const field = document.createElement('input');
    document.body.appendChild(field);
    field.focus();
    const base = term.focused;
    const { requestFocus } = await import('../src/store/terminal');
    requestFocus(false);
    await flushPromises();
    expect(term.focused).toBe(base);
    requestFocus(true);
    await flushPromises();
    expect(term.focused).toBe(base + 1);
    host.unmount();
  });
});

describe('56-5б панель: вкладки, закрытие, журнал, телефон', () => {
  it('«+» создаёт терминал, «×» просит подтверждения, потом удаляет', async () => {
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    await flushPromises();
    expect(document.body.querySelectorAll('.term-tab')).toHaveLength(1);
    (document.body.querySelector('[data-test="term-add"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelectorAll('.term-tab')).toHaveLength(2);
    (document.body.querySelector('[data-term-close="1"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('[data-test="term-close-ask"]')!.textContent).toContain('оболочка и всё в ней завершится');
    expect(sent.some((s) => s.method === 'DELETE')).toBe(false);
    (document.body.querySelector('[data-test="term-close-yes"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(sent.some((s) => s.method === 'DELETE' && s.path === '/api/v1/term/1')).toBe(true);
    expect(document.body.querySelectorAll('.term-tab')).toHaveLength(1);
    host.unmount();
  });

  it('свернуть в полосу, развернуть, журнал запусков', async () => {
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    await flushPromises();
    (document.body.querySelector('[data-test="term-collapse"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(termState.collapsed).toBe(true);
    expect(document.body.querySelector('.term-panel--collapsed')).not.toBeNull();
    (document.body.querySelector('[data-test="term-expand"]') as HTMLButtonElement).click();
    expect(termState.collapsed).toBe(false);
    (document.body.querySelector('[data-test="term-runs"]') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('[data-test="term-runs-list"]')!.textContent).toContain('ls');
    host.unmount();
  });

  it('телефон: ряд клавиш и поле «строка» без автоподстановок; «⏎» шлёт строку и Enter', async () => {
    layout.mode = 'phone';
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    await flushPromises();
    expect(document.body.querySelector('.term-panel--phone')).not.toBeNull();
    const labels = [...document.body.querySelectorAll('.term-key')].map((b) => b.textContent!.trim());
    expect(labels).toEqual(expect.arrayContaining(['Esc', 'Tab', 'Ctrl-C', 'Ctrl-D', '↑', '↓', '←', '→', '⏎']));
    const input = document.body.querySelector('[data-test="term-line"]') as HTMLInputElement;
    expect(input.getAttribute('autocomplete')).toBe('off');
    expect(input.getAttribute('autocapitalize')).toBe('off');
    expect(input.getAttribute('spellcheck')).toBe('false');
    input.value = 'ls -la';
    input.dispatchEvent(new Event('input'));
    (document.body.querySelector('[data-test="term-line-send"]') as HTMLButtonElement).click();
    await sleep(40);
    expect(atob(posts('/input').at(-1)!.body!.data_b64 as string)).toBe('ls -la\r');
    // xterm на телефоне — только показ и прокрутка.
    expect(mocks.MockTerminal.instances[0]!.options.disableStdin).toBe(true);
    expect(mocks.MockTerminal.instances[0]!.textarea.getAttribute('inputmode')).toBe('none');
    host.unmount();
  });
});

describe('56-5б кнопки открытия', () => {
  it('верхняя полоса: «⌨ Терминал» открывает, повторный клик сворачивает', async () => {
    terms = [info(1)];
    const bar = mount(TopBar, { attachTo: document.body });
    const btn = bar.find('[data-test="top-term"]');
    expect(btn.text()).toContain('⌨');
    await btn.trigger('click');
    await flushPromises();
    expect(termState.open).toBe(true);
    await btn.trigger('click');
    expect(termState.collapsed).toBe(true);
    bar.unmount();
  });

  it('меню «⋯ -> Терминал» и кнопка в шапке окна «Агент»', async () => {
    terms = [info(1)];
    store.state.projects = [];
    const menu = mount(TopMenu, { attachTo: document.body });
    await menu.find('.top-menu-button').trigger('click');
    const item = menu.find('.terminal-open');
    expect(item.text()).toBe('Терминал');
    await item.trigger('click');
    await flushPromises();
    expect(termState.open).toBe(true);
    menu.unmount();
    resetTerminal();

    const queue = mount(AgentQueue, { attachTo: document.body });
    await queue.find('[data-test="agent-terminal"]').trigger('click');
    await flushPromises();
    expect(termState.open).toBe(true);
    queue.unmount();
  });
});

describe('терминал на телефоне: подгонка под контейнер', () => {
  function container(w: number, h: number): void {
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => w });
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => h });
  }
  afterEach(() => {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientHeight;
  });

  it('снимок 180x45 в контейнере 390x600 -> resize под контейнер и scrollToBottom, даже без фокуса', async () => {
    container(390, 600);
    terms = [info(1)];
    vi.spyOn(document, 'hasFocus').mockReturnValue(false);
    layout.mode = 'phone';
    const host = await mountHost();
    await openTerminal();
    const term = await paneReady();
    await sleep(150);
    term.log.length = 0;
    FakeSource.all[0]!.emit('snapshot', { ...snapshot(1, 'aa:5'), cols: 180, rows: 45 });
    await sleep(150);
    expect(term.log.slice(0, 2)).toEqual(['reset', 'resize 180x45']);
    expect(term.log).toContain('resize 100x30');
    expect(term.log.at(-1)).toBe('scrollToBottom');
    expect(term.log.indexOf('scrollToBottom')).toBeGreaterThan(term.log.indexOf('resize 100x30'));
    host.unmount();
  });

  it('proposeDimensions первые 2 раза undefined -> подгонка повторяется и проходит', async () => {
    container(390, 600);
    mocks.fitState.undefinedFirst = 2;
    terms = [info(1)];
    const host = await mountHost();
    await openTerminal();
    const term = await paneReady();
    await sleep(700);
    expect(mocks.fitState.calls).toBeGreaterThanOrEqual(3);
    expect(term.log).toContain('resize 100x30');
    host.unmount();
  });
});
