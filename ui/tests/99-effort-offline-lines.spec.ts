// Усилие по движку, чип «модель · усилие», убранные линии «не в сети» (`hidden_lines` раскладки).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import type { OfflineLine } from '../src/api/types';
import BoardList from '../src/components/BoardList.vue';
import SessionModelChip from '../src/components/SessionModelChip.vue';
import {
  boardState,
  hideOfflineLine,
  hideOfflineLines,
  normalizeLayout,
  openColumn,
  resetBoard,
} from '../src/store/board';
import { isLineHidden, visibleOfflineLines } from '../src/util/boardList';
import { effortChoices, effortForEngine, modelChipText } from '../src/util/sessions';

function line(key: string, projectId: number, ended: number | null): OfflineLine {
  return {
    key,
    project_id: projectId,
    role: key,
    role_no: 1,
    engine: 'claude',
    session_name: null,
    cwd: null,
    ended_ts: ended,
    end_reason: null,
    pending: 0,
    sub_id: 1,
  };
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 404, headers: { 'content-type': 'application/json' } })));
  resetBoard();
});
afterEach(() => {
  resetBoard();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('усилие по движку', () => {
  it('уровни по движку; значение не из списка добавляется; сброс при смене движка', () => {
    expect(effortChoices('claude')).toEqual(['low', 'medium', 'high', 'xhigh', 'max']);
    expect(effortChoices('opencode')).toEqual(['low', 'high', 'max']);
    expect(effortChoices('opencode', 'weird')).toEqual(['low', 'high', 'max', 'weird']);
    expect(effortForEngine('opencode', 'xhigh')).toBe('');
    expect(effortForEngine('opencode', 'high')).toBe('high');
    expect(effortForEngine('claude', 'xhigh')).toBe('xhigh');
  });
});

describe('чип модели', () => {
  it('текст: модель · усилие; пусто — «модель по умолчанию»', () => {
    expect(modelChipText({ model: 'opus', effort: 'high' })).toBe('opus · high');
    expect(modelChipText({ model: 'opus', effort: null })).toBe('opus');
    expect(modelChipText({ model: null, effort: null })).toBe('модель по умолчанию');
    expect(modelChipText({ model: null, effort: 'max' })).toBe('модель · max');
  });

  it('кнопка с title и событием click', async () => {
    const w = mount(SessionModelChip, { props: { session: { model: 'x', effort: null } } });
    expect(w.attributes('title')).toBe('Модель и усилие');
    await w.trigger('click');
    expect(w.emitted('click')).toHaveLength(1);
  });
});

describe('убранные линии «не в сети»', () => {
  it('фильтр: запись с ended_ts >= линии скрывает, более новая линия видна опять', () => {
    const hidden = [{ key: 'a', ended_ts: 100 }];
    expect(isLineHidden(line('a', 1, 100), hidden)).toBe(true);
    expect(isLineHidden(line('a', 1, 90), hidden)).toBe(true);
    expect(isLineHidden(line('a', 1, 101), hidden)).toBe(false);
    expect(isLineHidden(line('b', 1, 50), hidden)).toBe(false);
    expect(visibleOfflineLines([line('a', 1, 100), line('b', 1, 5)], hidden).map((l) => l.key)).toEqual(['b']);
  });

  it('нормализация hidden_lines: мусор и дубли ключей выбрасываются', () => {
    const norm = normalizeLayout({ hidden_lines: [{ key: 'a', ended_ts: 1 }, { key: 'a', ended_ts: 2 }, { key: '', ended_ts: 1 }, { key: 'b' }, 'x'] });
    expect(norm.hidden_lines).toEqual([{ key: 'a', ended_ts: 1 }]);
  });

  it('hideOfflineLine заменяет запись по ключу и закрывает столбец; hideOfflineLines — все линии проекта', () => {
    boardState.offline = [line('a', 1, 100), line('b', 1, 200), line('c', 2, 300)];
    openColumn({ kind: 'agent', key: 'a' });
    hideOfflineLine(boardState.offline[0]!);
    hideOfflineLine({ key: 'a', ended_ts: 150 });
    expect(boardState.layout.hidden_lines).toEqual([{ key: 'a', ended_ts: 150 }]);
    expect(boardState.layout.open).toEqual([]);
    expect(boardState.dirty).toBe(true);
    hideOfflineLines(1);
    expect(boardState.layout.hidden_lines.map((h) => h.key).sort()).toEqual(['a', 'b']);
  });

  it('список: «×» убирает линию без открытия столбца; пункт меню проекта убирает все', async () => {
    boardState.offline = [line('a', 1, 100), line('b', 1, 200)];
    const { store } = await import('../src/store/instance');
    store.state.projects = [{ id: 1, name: 'П', color: null, sort: 0, muted: false, workdir: '/p' } as never];
    const w = mount(BoardList);
    expect(w.findAll('.board-row--agent')).toHaveLength(2);
    await w.get('.board-row-remove').trigger('click');
    expect(boardState.layout.open).toEqual([]);
    expect(w.findAll('.board-row--agent')).toHaveLength(1);
    await w.get('.board-group-menu').trigger('click');
    await w.get('.board-group-hide-offline').trigger('click');
    expect(w.findAll('.board-row--agent')).toHaveLength(0);
    await w.get('.board-group-menu').trigger('click');
    expect(w.find('.board-group-hide-offline').exists()).toBe(false);
    w.unmount();
  });
});
