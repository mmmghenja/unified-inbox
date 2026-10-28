// Ф77-1 (D): машина роли — тексты чипа, подсказки и окна «⋯ → Машины». Всё от ядра (имя, hostname) — чужой ввод:
// только текстом.
import type { Machine } from '../api/types';

const ROUTE_WORD: Record<string, string> = {
  core: 'машина ядра',
  lan: 'локальная сеть',
  s1: 'через relay',
  's1-socks': 'через relay (socks)',
};

/** Путь словами; неизвестный — `null`. */
export function routeWord(route: Machine['route']): string | null {
  return route === null ? null : (ROUTE_WORD[route] ?? null);
}

/** «2 мин назад» / «3 ч назад» / «5 д назад»; меньше минуты — «только что». */
export function agoText(ts: number, nowMs: number): string {
  const min = Math.max(0, Math.floor((nowMs - ts) / 60_000));
  if (min < 1) return 'только что';
  if (min < 60) return `${min} мин назад`;
  if (min < 24 * 60) return `${Math.floor(min / 60)} ч назад`;
  return `${Math.floor(min / 1440)} д назад`;
}

/** Подсказка чипа: hostname, путь словами, последний контакт — те части, что известны. */
export function machineTitle(machine: Machine, nowMs: number = Date.now()): string {
  const parts: string[] = [];
  if (machine.hostname) parts.push(machine.hostname);
  const route = routeWord(machine.route) ?? (machine.core ? 'машина ядра' : null);
  if (route !== null) parts.push(route);
  if (machine.last_ts !== null) parts.push(`последний контакт ${agoText(machine.last_ts, nowMs)}`);
  return parts.length === 0 ? machine.display : parts.join(' · ');
}

/** Строка над командой «Открыть в терминале»; машина неизвестна — `null`. */
export function terminalMachineLine(machine: Machine | null | undefined): string | null {
  return machine ? `Выполнить на машине «${machine.display}»` : null;
}

/** Версия машины отличается от версии ядра (обе известны). */
export function versionDiffers(machine: Machine, coreVersion: string | null): boolean {
  return !machine.core && machine.version !== null && coreVersion !== null && machine.version !== coreVersion;
}
