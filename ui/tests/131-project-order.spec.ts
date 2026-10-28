// Ф99-8: очерёдность проектов — помощник порядка и кнопки «↑ / ↓» в «Раскладке».
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { Project } from '../src/api/types';
import ProjectOrderBlock from '../src/components/ProjectOrderBlock.vue';
import { store } from '../src/store/instance';
import { movedIds, orderedProjects, projectRank } from '../src/util/projectOrder';

function project(id: number, name: string, sort: number): Project {
  return { id, name, color: null, sort, muted: false, workdir: null, unread: 0 } as Project;
}

const PROJECTS: Project[] = [project(1, 'А', 2), project(2, 'Б', 0), project(3, 'В', 1), project(-1, 'Неразобранное', 0)];

describe('помощник порядка', () => {
  it('сортирует по sort, служебные не участвуют', () => {
    expect(orderedProjects(PROJECTS).map((p) => p.id)).toEqual([2, 3, 1]);
    const rank = projectRank(PROJECTS);
    expect(rank(2)).toBeLessThan(rank(3));
    expect(rank(3)).toBeLessThan(rank(1));
    expect(rank(1)).toBeLessThan(rank(999));
    expect(rank(0)).toBeGreaterThan(rank(999));
    expect(rank(null)).toBeGreaterThan(rank(999));
  });

  it('сдвиг на месте, на краю без изменений', () => {
    expect(movedIds([2, 3, 1], 3, -1)).toEqual([3, 2, 1]);
    expect(movedIds([2, 3, 1], 3, 1)).toEqual([2, 1, 3]);
    expect(movedIds([2, 3, 1], 2, -1)).toEqual([2, 3, 1]);
    expect(movedIds([2, 3, 1], 1, 1)).toEqual([2, 3, 1]);
  });
});

describe('блок «Порядок проектов»', () => {
  beforeEach(() => {
    store.state.projects = PROJECTS;
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('«↓» отправляет новый порядок и перечитывает проекты', async () => {
    const put = vi.spyOn(api, 'putProjectOrder').mockResolvedValue({ ok: true });
    const load = vi.spyOn(store, 'loadProjects').mockResolvedValue(undefined as never);
    const w = mount(ProjectOrderBlock);
    expect(w.findAll('.project-order-row').map((r) => r.attributes('data-project-id'))).toEqual(['2', '3', '1']);
    expect(w.find('[data-project-id="2"] .project-order-up').attributes('disabled')).toBeDefined();
    expect(w.find('[data-project-id="1"] .project-order-down').attributes('disabled')).toBeDefined();
    await w.find('[data-project-id="2"] .project-order-down').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith([3, 2, 1]);
    expect(load).toHaveBeenCalled();
  });

  it('«↑» двигает вверх; отказ ядра показывается', async () => {
    const put = vi.spyOn(api, 'putProjectOrder').mockRejectedValue(new Error('x'));
    vi.spyOn(store, 'loadProjects').mockResolvedValue(undefined as never);
    const w = mount(ProjectOrderBlock);
    await w.find('[data-project-id="1"] .project-order-up').trigger('click');
    await flushPromises();
    expect(put).toHaveBeenCalledWith([2, 1, 3]);
    expect(w.find('[role="alert"]').exists()).toBe(true);
  });
});
