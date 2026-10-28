// Цвет проекта (`project.color`) — недоверенное значение из БД (задаётся вручную, но незачем доверять
// формату). Спека: применять через `:style` (CSSOM), не через атрибут в шаблоне-строке, и валидировать
// регуляркой перед применением — иначе не применять вовсе.
const COLOR_RE = /^#[0-9a-fA-F]{3,8}$/;

export function safeColor(color: string | null | undefined): string | undefined {
  return typeof color === 'string' && COLOR_RE.test(color) ? color : undefined;
}
