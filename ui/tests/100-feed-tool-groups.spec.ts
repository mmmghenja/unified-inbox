// Окно сессии: идущие подряд шаги инструментов — одной свёрнутой строкой (`groupSteps`); прочие события — как есть.
import { describe, expect, it } from 'vitest';

import { groupSteps } from '../src/util/sessionSteps';

const e = (id: number, kind: string, tool: string | null = null, is_error = false) => ({ id, kind, tool, is_error });

describe('groupSteps', () => {
  it('подряд идущие tool/tool_done — одна строка с числом шагов и именами', () => {
    const rows = groupSteps([
      e(1, 'say'),
      e(2, 'tool', 'Read'),
      e(3, 'tool_done'),
      e(4, 'tool', 'Read'),
      e(5, 'tool_done'),
      e(6, 'tool', 'Bash'),
      e(7, 'tool_done', null, true),
      e(8, 'text'),
      e(9, 'tool', 'Edit'),
    ]);
    expect(rows.map((r) => r.type)).toEqual(['event', 'tools', 'event', 'event']);
    const first = rows[1]!;
    expect(first.type === 'tools' && [first.id, first.text, first.errors, first.events.length]).toEqual([
      2,
      '3 шага · Read ×2, Bash',
      1,
      6,
    ]);
    // Один шаг — обычной строкой, без обёртки.
    const last = rows[3]!;
    expect(last.type === 'event' && last.event.id).toBe(9);
  });

  it('без инструментов — строка на событие; пусто — пусто', () => {
    expect(groupSteps([e(1, 'say'), e(2, 'text')]).map((r) => r.type)).toEqual(['event', 'event']);
    expect(groupSteps([])).toEqual([]);
  });
});
