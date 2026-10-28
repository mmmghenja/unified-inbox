// Ф72-1 (`docs/architecture.md`): ссылки на MD-файлы в тексте модели и окно просмотра.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../src/api/client';
import MdViewerModal from '../src/components/MdViewerModal.vue';
import ModelText from '../src/components/ModelText.vue';
import { renderModelMarkdown } from '../src/render/markdown';
import { mdClose, mdViewer, openMd } from '../src/store/mdViewer';

const mounted: { unmount(): void }[] = [];
function mountIt<T extends { unmount(): void }>(w: T): T {
  mounted.push(w);
  return w;
}

const files = (html: string): string[] =>
  Array.from(new DOMParser().parseFromString(html, 'text/html').querySelectorAll('a.md-file')).map(
    (a) => a.getAttribute('data-md-path') ?? '',
  );

describe('Ф72-1 A: разметка путей', () => {
  it('ссылка markdown с не-http адресом на .md', () => {
    const html = renderModelMarkdown('см. [план](docs/architecture.md#раздел) и [строка](/home/u/a.md:12)');
    expect(files(html)).toEqual(['docs/architecture.md#раздел', '/home/u/a.md:12']);
    expect(html).not.toContain('href=');
  });

  it('код в строке и голые пути', () => {
    const html = renderModelMarkdown(
      '`~/notes/a.md` и /home/u/b.md, ploshchadki/issledovanie-2026-10-19.md, docs/architecture.md. Ещё readme.md',
    );
    expect(files(html)).toEqual([
      '~/notes/a.md',
      '/home/u/b.md',
      'ploshchadki/issledovanie-2026-10-19.md',
      'docs/architecture.md',
      'readme.md',
    ]);
    expect(html).not.toContain('href=');
  });

  it('блоки кода не трогаются, http-ссылки как были, не-md ссылки без href', () => {
    const html = renderModelMarkdown('```\ncat docs/architecture.md\n```\n\n[сайт](https://e.com/a.md) [js](javascript:alert(1)) a.txt');
    expect(files(html)).toEqual([]);
    expect(html).toContain('href="https://e.com/a.md"');
    expect(html).not.toContain('href="javascript');
  });

  it('очистка: чужие data-*, класс и javascript: в пути — только строка', () => {
    expect(files(renderModelMarkdown('[x](javascript:1.md)'))).toEqual([]);
    const raw = renderModelMarkdown('<a class="md-file" data-md-path="x.md" data-evil="1" onclick="alert(1)">y</a>');
    const doc = new DOMParser().parseFromString(raw, 'text/html');
    expect(doc.querySelector('[onclick], [data-evil]')).toBeNull();
    const html = renderModelMarkdown('[план](a.md)');
    expect(html).not.toMatch(/data-(?!md-path)/);
    expect(html).toContain('class="md-file"');
  });
});

describe('Ф72-1 B: окно просмотра', () => {
  const getMd = vi.fn();
  beforeEach(() => {
    getMd.mockReset();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = new URL(url, 'http://x');
        const r = getMd(u.searchParams);
        if (r instanceof ApiError) {
          return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } });
        }
        return new Response(JSON.stringify(r), { status: 200, headers: { 'content-type': 'application/json' } });
      }),
    );
  });
  afterEach(() => {
    mdClose();
    while (mounted.length > 0) mounted.pop()!.unmount();
    document.body.innerHTML = '';
    vi.unstubAllGlobals();
  });

  it('загрузка, текст, ссылка внутри файла и «назад»', async () => {
    getMd.mockImplementation((q: URLSearchParams) =>
      q.get('path') === 'b.md'
        ? { path: '/p/b.md', text: '# Вторая', size: 1, mtime_ms: 1 }
        : { path: '/p/a.md', text: '# Первая\n\nсм. b.md', size: 1, mtime_ms: 1 },
    );
    mountIt(mount(MdViewerModal, { attachTo: document.body }));
    openMd('a.md', { sessionId: 5, projectId: 2 });
    await flushPromises();
    expect(document.body.textContent).not.toContain('загружаю');
    expect(document.body.querySelector('.md h1')?.textContent).toBe('Первая');
    expect(getMd.mock.calls[0]![0].get('session_id')).toBe('5');
    expect(getMd.mock.calls[0]![0].get('project_id')).toBe('2');

    (document.body.querySelector('a.md-file') as HTMLElement).click();
    await flushPromises();
    expect(document.body.querySelector('.md h1')?.textContent).toBe('Вторая');
    const q = getMd.mock.calls[1]![0] as URLSearchParams;
    expect(q.get('path')).toBe('b.md');
    expect(q.get('from')).toBe('/p/a.md');
    expect(q.get('session_id')).toBe('5');

    (document.body.querySelector('.md-viewer-back') as HTMLElement).click();
    await flushPromises();
    expect(document.body.querySelector('.md h1')?.textContent).toBe('Первая');
    expect(mdViewer.stack).toHaveLength(1);
  });

  it('404 — список tried, 413 — сообщение', async () => {
    getMd.mockReturnValueOnce(
      new ApiError(404, 'x', { detail: { code: 'md_not_found', path: 'z.md', tried: ['/a/z.md', '/b/z.md'] } }),
    );
    mountIt(mount(MdViewerModal, { attachTo: document.body }));
    openMd('z.md');
    await flushPromises();
    expect(document.body.textContent).toContain('файл не найден: z.md');
    expect(document.body.textContent).toContain('/b/z.md');

    getMd.mockReturnValueOnce(new ApiError(413, 'x', { detail: { code: 'md_too_big' } }));
    openMd('big.md');
    await flushPromises();
    expect(document.body.textContent).toContain('файл больше 2 МиБ');
  });

  it('ModelText: клик по ссылке на файл открывает окно с контекстом', async () => {
    getMd.mockReturnValue({ path: '/p/x.md', text: 'тело', size: 1, mtime_ms: 1 });
    mountIt(mount(MdViewerModal, { attachTo: document.body }));
    const w = mountIt(
      mount(ModelText, { props: { text: 'см. docs/architecture.md', sessionId: 7, projectId: 3 }, attachTo: document.body }),
    );
    (w.get('a.md-file').element as HTMLElement).click();
    await flushPromises();
    const q = getMd.mock.calls[0]![0] as URLSearchParams;
    expect(q.get('path')).toBe('docs/architecture.md');
    expect(q.get('session_id')).toBe('7');
    expect(q.get('project_id')).toBe('3');
    expect(document.body.querySelector('.md-viewer')).not.toBeNull();
  });
});
