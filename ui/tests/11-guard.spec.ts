// Пункт 11 спеки: сторож на опасные API в `ui/src` — грепом исходников, а не только eslint-ом (чтобы
// отключающий комментарий не мог тихо провалить защиту). Список — из спеки 06 плюс три добавленных
// архитектором слова: `localStorage`, `sessionStorage`, `document.cookie`.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SRC_DIR = join(__dirname, '..', 'src');
// Ф10-1 B: markdown ответов моделей — единственный компонент с сырым HTML (см. его комментарий и тест 31).
const MODEL_TEXT = join(SRC_DIR, 'components', 'ModelText.vue');
// Ф10-6, решение 7: флаги интерфейса (свёрнут ли оркестратор) — единственное место с `localStorage`: ровно одно
// чтение и одна запись `window.localStorage` флага `'1'/'0'`, больше ничего.
const UI_PREFS = join(SRC_DIR, 'util', 'uiPrefs.ts');

const FORBIDDEN: RegExp[] = [
  /v-html/i,
  /\.innerHTML\b/,
  /\binnerHTML\s*[:=]/,
  /\.outerHTML\b/,
  /\bouterHTML\s*[:=]/,
  /insertAdjacentHTML/,
  /document\.write\s*\(/,
  /\beval\s*\(/,
  /new\s+Function\s*\(/,
  /\blocalStorage\b/,
  /\bsessionStorage\b/,
  /document\.cookie/,
];

function listFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      out.push(...listFiles(full));
    } else if (/\.(ts|vue|tsx|js)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

describe('11 Сторож: запрещённые API отсутствуют в ui/src', () => {
  const files = listFiles(SRC_DIR);

  it('в дереве ui/src есть исходники (сторож не проверяет пустоту)', () => {
    expect(files.length).toBeGreaterThan(5);
  });

  it.each(files.map((f) => [f.slice(SRC_DIR.length + 1), f] as const))(
    '%s не содержит запрещённых конструкций',
    (_rel, full) => {
      let content = readFileSync(full, 'utf8');
      if (full === MODEL_TEXT) {
        // Ф10-1 B: единственное исключение — ровно один `v-html="html"` (очищенный markdown), больше ничего.
        expect(content.match(/v-html\s*=/g)?.length ?? 0).toBe(1);
        expect(content).toContain('v-html="html"');
        content = content.replace('v-html="html"', '').replace(/vue\/no-v-html/g, '').replace(/v-html/g, '');
      }
      if (full === UI_PREFS) {
        const code = content.replace(/^\s*\/\/.*$/gm, '');
        // Ф12-5 C2: плюс чтение и запись id аккаунта «Откуда» (целое, `String(value)`) — ровно по одному.
        expect(code.match(/\blocalStorage\b/g)).toEqual(Array(6).fill('localStorage'));
        // Ф34-1: плюс короткая строка выбора (фильтр стадий проекта) — чтение и запись по одному.
        expect(code.match(/window\.localStorage\.getItem\(key\)/g)).toHaveLength(3);
        expect(code).toContain('window.localStorage.setItem(key, value)');
        expect(code).toContain("window.localStorage.setItem(key, value ? '1' : '0')");
        expect(code).toContain('window.localStorage.setItem(key, String(value))');
        content = code
          .replace(/window\.localStorage\.getItem\(key\)/g, '')
          .replace("window.localStorage.setItem(key, value ? '1' : '0')", '')
          .replace('window.localStorage.setItem(key, String(value))', '')
          .replace('window.localStorage.setItem(key, value)', '');
      }
      for (const pattern of FORBIDDEN) {
        expect(pattern.test(content), `${full} matched ${pattern}`).toBe(false);
      }
    },
  );
});
