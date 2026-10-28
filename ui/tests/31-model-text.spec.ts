// Ф10-1 B (`docs/architecture.md` 2–4): markdown ответов моделей через `ModelText` —
// оформление, XSS-набор (сырой HTML, опасные схемы ссылок, картинки, атрибуты), где рендерится и где нет.
// Проверяется РЕАЛЬНЫЙ смонтированный DOM.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { WorkEvent } from '../src/api/types';
import ModelText from '../src/components/ModelText.vue';
import WorkEventFeed from '../src/components/WorkEventFeed.vue';
import { renderModelMarkdown } from '../src/render/markdown';

const NOW = new Date(2026, 8, 25, 12, 0, 0).getTime();

function mountText(text: string, collapsible = false) {
  return mount(ModelText, { props: { text, collapsible }, attachTo: document.body });
}

function body(wrapper: ReturnType<typeof mountText>): Element {
  return wrapper.get('.md').element;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
  delete (window as unknown as { __xss?: number }).__xss;
});

// --- оформление -------------------------------------------------------------------------------------------------

describe('Ф10-1 B: ModelText — оформление', () => {
  it('заголовок, список, жирный', () => {
    const w = mountText('## План\n\n- **раз**\n- два\n\n1. первый');
    const root = body(w);
    expect(root.querySelector('h2')?.textContent).toBe('План');
    expect(Array.from(root.querySelectorAll('ul > li')).map((li) => li.textContent)).toEqual(['раз', 'два']);
    expect(root.querySelector('ul strong')?.textContent).toBe('раз');
    expect(root.querySelector('ol > li')?.textContent).toBe('первый');
    w.unmount();
  });

  it('блок кода — pre>code с кнопкой «копировать»; клик копирует текст блока', async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal('isSecureContext', true); // Ф10-7: `navigator.clipboard` — только в безопасном контексте
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const w = mountText('Команда:\n\n```bash\nuv run pytest -q\n<b>x</b>\n```');
    const code = body(w).querySelector('.md-code pre > code');
    expect(code?.textContent).toBe('uv run pytest -q\n<b>x</b>\n');
    expect(code?.getAttribute('class')).toBeNull();
    const button = body(w).querySelector('.md-code > button.md-copy');
    expect(button?.getAttribute('type')).toBe('button');
    (button as HTMLButtonElement).click();
    await flushPromises();
    expect(writeText).toHaveBeenCalledWith('uv run pytest -q\n<b>x</b>\n');
    expect(button?.textContent).toBe('скопировано');
    w.unmount();
  });

  it('буфер недоступен — тихо, кнопка пишет «не вышло»', async () => {
    vi.stubGlobal('isSecureContext', true);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn(async () => Promise.reject(new Error('denied'))) },
      configurable: true,
    });
    const w = mountText('```\nx\n```');
    const button = body(w).querySelector('button.md-copy') as HTMLButtonElement;
    button.click();
    await flushPromises();
    expect(button.textContent).toBe('не вышло');
    w.unmount();
  });

  it('таблица — в .md-table, без style у ячеек', () => {
    const w = mountText('| a | b |\n|:--|--:|\n| 1 | 2 |');
    const table = body(w).querySelector('div.md-table > table');
    expect(table).not.toBeNull();
    expect(Array.from(table!.querySelectorAll('td')).map((td) => td.textContent)).toEqual(['1', '2']);
    expect(body(w).querySelectorAll('[style]').length).toBe(0);
    w.unmount();
  });

  it('ссылка https — target=_blank, rel=noopener noreferrer; linkify', () => {
    const w = mountText('[доки](https://example.com/a) и https://example.org/b');
    const links = Array.from(body(w).querySelectorAll('a'));
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['https://example.com/a', 'https://example.org/b']);
    for (const a of links) {
      expect(a.getAttribute('target')).toBe('_blank');
      expect(a.getAttribute('rel')).toBe('noopener noreferrer');
    }
    w.unmount();
  });

  it('breaks: одиночный перенос строки — <br>', () => {
    const w = mountText('первая\nвторая');
    expect(body(w).querySelector('p')?.innerHTML).toBe('первая<br>\nвторая');
    w.unmount();
  });

  it('collapsible: высокое — свёрнуто с «показать полностью» / «свернуть»; без collapsible — нет', async () => {
    const height = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(1000);
    const w = mountText('длинно', true);
    await flushPromises();
    expect(w.get('.md').classes()).toContain('md--collapsed');
    await w.get('button.md-toggle').trigger('click');
    expect(w.get('.md').classes()).not.toContain('md--collapsed');
    expect(w.get('button.md-toggle').text()).toBe('свернуть');
    w.unmount();
    const plain = mountText('длинно', false);
    await flushPromises();
    expect(plain.find('button.md-toggle').exists()).toBe(false);
    expect(plain.get('.md').classes()).not.toContain('md--collapsed');
    plain.unmount();
    height.mockReturnValue(100);
    const short = mountText('коротко', true);
    await flushPromises();
    expect(short.find('button.md-toggle').exists()).toBe(false);
    short.unmount();
  });
});

// --- XSS ----------------------------------------------------------------------------------------------------------

const XSS_CASES = [
  '<script>window.__xss=1</script>',
  '<img src=x onerror="window.__xss=1">',
  '<svg onload="window.__xss=1"><circle/></svg>',
  '<iframe src="https://evil.example/"></iframe>',
  '[x](javascript:alert(1))',
  '[x](JAVASCRIPT:alert(1))',
  '[x](data:text/html,<script>alert(1)</script>)',
  '[x](vbscript:msgbox(1))',
  '[x](file:///etc/passwd)',
  '[x](//evil.example/)',
  '[x](/api/v1/outbox/1/approve)',
  '![i](https://evil.example/p.png)',
  'javascript:alert(1) и www.example.com и mailto:a@b.c',
  '<javascript:alert(1)>',
  '[x](https://a.b "\\" onmouseover=\\"window.__xss=1")',
  '<a href="javascript:alert(1)">x</a>',
  '<div style="position:fixed;inset:0">x</div>',
  '| a |\n|:-:|\n| <img src=x onerror=alert(1)> |',
  '```js" onclick="alert(1)\ncode\n```',
];

function assertSafe(root: Element): void {
  expect(root.querySelectorAll('script, img, svg, iframe, style, object, embed, form, input').length).toBe(0);
  for (const el of root.querySelectorAll('*')) {
    for (const attr of Array.from(el.attributes)) {
      expect(attr.name.toLowerCase().startsWith('on'), `${el.tagName} ${attr.name}`).toBe(false);
      expect(attr.name.toLowerCase(), el.tagName).not.toBe('style');
      expect(attr.name.toLowerCase(), el.tagName).not.toBe('src');
    }
    if (el.tagName === 'A' && el.hasAttribute('href')) {
      expect(el.getAttribute('href')).toMatch(/^https?:\/\//);
    }
  }
}

describe('Ф10-1 B: ModelText — XSS', () => {
  it.each(XSS_CASES)('%s', async (payload) => {
    const w = mountText(payload);
    await flushPromises();
    assertSafe(body(w));
    // Та же строка, разобранная отдельно от компонента (сырой текст `<…>` в ней экранирован как `&lt;`).
    const html = renderModelMarkdown(payload);
    expect(html).not.toMatch(/<(script|img|svg|iframe|style)\b/i);
    assertSafe(new DOMParser().parseFromString(`<body>${html}</body>`, 'text/html').body);
    expect((window as unknown as { __xss?: number }).__xss).toBeUndefined();
    w.unmount();
  });

  it('опасные ссылки остаются текстом, не ссылкой', () => {
    for (const payload of ['[x](javascript:alert(1))', '[x](//evil.example/)', '[x](/api/v1/outbox/1/approve)']) {
      const root = body(mountText(payload));
      expect(root.querySelector('a'), payload).toBeNull();
      expect(root.textContent?.trim()).toBe(payload);
    }
  });

  it('картинка — не <img>, внешней загрузки нет', () => {
    const root = body(mountText('![i](https://evil.example/p.png)'));
    expect(root.querySelector('img')).toBeNull();
    expect(root.textContent).toContain('!');
  });

  it('заголовок ссылки с кавычками — только атрибут title', () => {
    const root = body(mountText('[x](https://a.b "\\" onmouseover=\\"window.__xss=1")'));
    const a = root.querySelector('a');
    expect(a?.getAttribute('href')).toBe('https://a.b');
    expect(Array.from(a!.attributes).map((x) => x.name).sort()).toEqual(['href', 'rel', 'target', 'title']);
  });
});

// --- где рендерится --------------------------------------------------------------------------------------------

function workEvent(id: number, kind: WorkEvent['kind'], summary: string): WorkEvent {
  return { id, ts: NOW, kind, tool: kind === 'tool' ? 'Bash' : null, summary, is_error: false, ref: null };
}

describe('Ф10-1 B: где markdown, где текст', () => {
  it('события сессии: `text` — HTML (свёртка включена), `tool` и `say` — буквально', async () => {
    const events = [workEvent(1, 'text', '**x**'), workEvent(2, 'tool', '**x**'), workEvent(3, 'say', 'владелец (next): **x**')];
    const w = mount(WorkEventFeed, { props: { events } });
    await flushPromises();
    const rows = w.findAll('.work-event');
    expect(rows[0]!.get('strong').text()).toBe('x');
    expect(rows[1]!.find('strong').exists()).toBe(false);
    expect(rows[1]!.text()).toBe('▸ Bash: **x**');
    expect(rows[2]!.find('strong').exists()).toBe(false);
    expect(rows[2]!.text()).toBe('ты написал: **x**');
    w.unmount();
  });
});
