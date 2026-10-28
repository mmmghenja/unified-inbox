// Ф40-4 A1 (`docs/architecture.md`): у каких источников текст сообщений — Markdown. Сейчас один —
// Mattermost (Telegram и Slack отдают разметку списком `entities_json`, почта — простой текст). Сообщения таких
// бесед рисует готовый безопасный рендерер `render/markdown.ts` (без HTML и картинок, ссылки только http/https);
// превью в списках и на главной остаются сырым текстом.
const MARKDOWN_SOURCES: ReadonlySet<string> = new Set(['mattermost']);

export function isMarkdownSource(source: string | null | undefined): boolean {
  return source !== null && source !== undefined && MARKDOWN_SOURCES.has(source);
}
