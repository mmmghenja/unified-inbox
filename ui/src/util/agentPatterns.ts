// Ф76-4б: чистые функции раздела «Мой агент» (образцы решений владельца, журнал решений агента).
import type { DecisionPattern } from '../api/types';

/** Основание решения агента по-русски: `rule:` → «по правилу», `pattern:<ключ>` → «по образцу …» и т. д. */
export function basisLabel(basis: string | null | undefined): string {
  const b = (basis ?? '').trim();
  if (b === '' || b === 'none') return 'без основания';
  if (b === 'card') return 'из карточки';
  const i = b.indexOf(':');
  const head = i < 0 ? b : b.slice(0, i);
  const tail = i < 0 ? '' : b.slice(i + 1).trim();
  if (head === 'rule') return tail === '' ? 'по правилу' : `по правилу ${tail}`;
  if (head === 'pattern') return tail === '' ? 'по образцу' : `по образцу ${tail}`;
  if (head === 'similar') return tail === '' ? 'по похожему решению' : `по похожему решению ${tail}`;
  return b;
}

const STATUS: Record<string, string> = {
  active: 'действует',
  young: 'молодой',
  faded: 'выцвел',
  dropped: 'убран',
};

export function patternStatusLabel(status: string): string {
  return STATUS[status] ?? status;
}

/** Полный новый список ключей: добавить ключ или убрать, если он уже там (ядро заменяет список целиком). */
export function toggleKey(list: string[], key: string): string[] {
  return list.includes(key) ? list.filter((k) => k !== key) : [...list, key];
}

export function exampleText(e: NonNullable<DecisionPattern['examples']>[number]): string {
  if (typeof e === 'string') return e;
  return [e.ask ?? e.text ?? '', e.answer ?? ''].filter((x) => x !== '').join(' → ');
}

export function clockLabel(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getDate())}.${p(d.getMonth() + 1)} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
