// Ф10-1 B: markdown ответов моделей (оркестратор, текст сессии, итог задачи) -> HTML для `ModelText.vue`.
// Текст моделей пересказывает чужие беседы — это недоверенный ввод, а страница несёт кнопки «Го» и «Разрешить».
// Две линии защиты, обе обязательны:
//  1) `markdown-it` с `html: false` (сырой HTML — текстом), без картинок, ссылки — только абсолютные http(s);
//  2) `DOMPurify` со списком тегов/атрибутов и хуком, который по тегу оставляет только нужные атрибуты
//     (`href` — снова только `^https?://`, `target/rel` проставляются, у `button` — только `type=button` и
//     `class=md-copy`/`md-run`). Атрибут `style` не разрешён нигде (выравнивание ячеек таблиц уходит).
// Третья линия — CSP ядра (`script-src 'self'`, без `unsafe-inline`).
import DOMPurify from 'dompurify';
import MarkdownIt from 'markdown-it';
import type { StateCore, Token } from 'markdown-it';

const SAFE_HREF = /^https?:\/\//i;

const ALLOWED_TAGS = [
  'p', 'br', 'strong', 'em', 'del', 's', 'code', 'pre', 'blockquote', 'ul', 'ol', 'li',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'hr', 'div',
  'button', 'span',
];
const ALLOWED_ATTR = ['href', 'target', 'rel', 'class', 'type', 'title', 'data-md-path'];

// Атрибуты, которые остаются у тега после очистки (всё прочее снимается хуком).
const ATTRS_BY_TAG: Record<string, readonly string[]> = {
  A: ['href', 'target', 'rel', 'title', 'class', 'data-md-path'],
  BUTTON: ['type', 'class'],
  DIV: ['class'],
};
const DIV_CLASSES = new Set(['md-table', 'md-code']);

export const COPY_LABEL = 'копировать';
// Ф56-5б (К3): «▶ в терминал» — только когда вызывающий явно сказал `runnable` (текст модели: ответ сессии, итог
// задачи, ответ агента). У сообщений людей, черновиков и живого текста кнопки нет.
export const RUN_LABEL = '▶ в терминал';
const BUTTON_CLASSES = new Set(['md-copy', 'md-run']);

// Ф72-1 A: путь к MD-файлу в тексте модели — кликабельный `<a class="md-file" data-md-path="…">` без `href`
// (открывает окно просмотра, `store/mdViewer.ts`). `data-md-path` — просто строка для запроса к ядру.
export const MD_FILE_CLASS = 'md-file';
const PATH_CHARS = String.raw`[\p{L}\p{N}_.\-%@+]`;
const PATH_BODY = String.raw`(?:file://|~/|/)?(?:${PATH_CHARS}+/)*${PATH_CHARS}*\.(?:md|markdown)(?::\d+|#[\p{L}\p{N}_\-]+)?`;
const MD_PATH_FULL = new RegExp(`^${PATH_BODY}$`, 'iu');
const MD_PATH_BARE = new RegExp(`(?<![\\p{L}\\p{N}_/.~@:%-])${PATH_BODY}(?![\\p{L}\\p{N}_/])`, 'giu');

/** Адрес без схемы, оканчивающийся на `.md`/`.markdown` (можно с `#якорем` / `:строкой`). */
export function isMdPath(value: string): boolean {
  return MD_PATH_FULL.test(value);
}

function decodePath(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

interface RenderEnv {
  runnable?: boolean;
}

const md = new MarkdownIt({ html: false, linkify: true, breaks: true, typographer: false });
md.disable('image');
md.validateLink = (url: string): boolean => SAFE_HREF.test(url) || isMdPath(url);

const escapeHtml = md.utils.escapeHtml;

function codeBlock(content: string, env: unknown): string {
  const run = (env as RenderEnv | undefined)?.runnable === true ? `<button type="button" class="md-run">${RUN_LABEL}</button>` : '';
  return (
    `<div class="md-code"><button type="button" class="md-copy">${COPY_LABEL}</button>${run}` +
    `<pre><code>${escapeHtml(content)}</code></pre></div>\n`
  );
}

// Блок кода — без класса языка из info-строки (подсветки нет), с кнопкой «копировать».
md.renderer.rules.fence = (tokens, idx, _options, env: unknown) => codeBlock(tokens[idx]!.content, env);
md.renderer.rules.code_block = (tokens, idx, _options, env: unknown) => codeBlock(tokens[idx]!.content, env);
// Таблица — в обёртке с прокруткой вбок.
md.renderer.rules.table_open = () => '<div class="md-table"><table>\n';
md.renderer.rules.table_close = () => '</table></div>\n';

function toMdLink(Token: StateCore['Token'], path: string): [Token, Token] {
  const open = new Token('link_open', 'a', 1);
  open.attrs = [
    ['class', MD_FILE_CLASS],
    ['data-md-path', path],
  ];
  return [open, new Token('link_close', 'a', -1)];
}

// Пути к MD-файлам вне ссылок: в обычном тексте и в коде в строке. Блоки кода (fence) не трогаются.
md.core.ruler.push('md_files', (state) => {
  for (const block of state.tokens) {
    if (block.type !== 'inline' || !block.children) continue;
    const out: Token[] = [];
    let inLink = false;
    const kids = block.children;
    for (let i = 0; i < kids.length; i += 1) {
      const tok = kids[i]!;
      if (tok.type === 'link_open') {
        const href = String(tok.attrGet('href') ?? '');
        const label = kids[i + 1]?.type === 'text' ? kids[i + 1]!.content : '';
        const isMd = !SAFE_HREF.test(href) && isMdPath(decodePath(href));
        // Linkify мог принять `имя.md` (домен Молдавии) за сайт: ссылка `http://имя.md` с таким же текстом.
        const fuzzy = tok.markup === 'linkify' && isMdPath(label) && href.replace(/^https?:\/\//i, '') === label;
        if (isMd || fuzzy) {
          tok.attrs = [
            ['class', MD_FILE_CLASS],
            ['data-md-path', isMd ? decodePath(href) : label],
          ];
        }
        inLink = true;
        out.push(tok);
      } else if (tok.type === 'link_close') {
        inLink = false;
        out.push(tok);
      } else if (!inLink && tok.type === 'code_inline' && isMdPath(tok.content.trim())) {
        const [open, close] = toMdLink(state.Token, tok.content.trim());
        out.push(open, tok, close);
      } else if (!inLink && tok.type === 'text' && /\.(?:md|markdown)/i.test(tok.content)) {
        let last = 0;
        for (const m of tok.content.matchAll(MD_PATH_BARE)) {
          const at = m.index ?? 0;
          if (at > last) {
            const t = new state.Token('text', '', 0);
            t.content = tok.content.slice(last, at);
            out.push(t);
          }
          const [open, close] = toMdLink(state.Token, m[0]);
          const t = new state.Token('text', '', 0);
          t.content = m[0];
          out.push(open, t, close);
          last = at + m[0].length;
        }
        if (last === 0) out.push(tok);
        else if (last < tok.content.length) {
          const t = new state.Token('text', '', 0);
          t.content = tok.content.slice(last);
          out.push(t);
        }
      } else {
        out.push(tok);
      }
    }
    block.children = out;
  }
  return true;
});

const purify = DOMPurify(window);
purify.addHook('afterSanitizeAttributes', (node) => {
  if (!(node instanceof Element)) return;
  const tag = node.tagName;
  const keep = ATTRS_BY_TAG[tag] ?? [];
  for (const attr of Array.from(node.attributes)) {
    if (!keep.includes(attr.name)) node.removeAttribute(attr.name);
  }
  if (tag === 'A') {
    const href = node.getAttribute('href');
    const mdPath = node.getAttribute('data-md-path');
    if (node.getAttribute('class') === MD_FILE_CLASS && mdPath !== null && mdPath !== '') {
      // Файловая ссылка: только класс и путь, без `href`.
      node.removeAttribute('href');
      node.removeAttribute('target');
      node.removeAttribute('rel');
      return;
    }
    node.removeAttribute('class');
    node.removeAttribute('data-md-path');
    if (href === null || !SAFE_HREF.test(href)) {
      node.removeAttribute('href');
      node.removeAttribute('target');
      node.removeAttribute('rel');
    } else {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  } else if (tag === 'BUTTON') {
    node.setAttribute('type', 'button');
    const cls = node.getAttribute('class');
    node.setAttribute('class', cls !== null && BUTTON_CLASSES.has(cls) ? cls : 'md-copy');
  } else if (tag === 'DIV') {
    const cls = node.getAttribute('class');
    if (cls !== null && !DIV_CLASSES.has(cls)) node.removeAttribute('class');
  }
});

/** Markdown модели -> очищенный HTML (строка для `ModelText.vue`). */
export function renderModelMarkdown(text: string, opts: { runnable?: boolean } = {}): string {
  const raw = md.render(text ?? '', { runnable: opts.runnable === true } satisfies RenderEnv);
  return purify.sanitize(raw, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
}
