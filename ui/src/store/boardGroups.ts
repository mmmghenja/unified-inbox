// Ф47-1: группы списка слева («Главная», компьютер) для видов «По проектам» и «Один проект» — те же данные и тот же
// расчёт (`buildBoardList`), что у `BoardList`; здесь они нужны ещё и рядам столбцов, поэтому собраны в одном месте.
import { computed, type ComputedRef } from 'vue';

import { boardState } from './board';
import { store } from './instance';
import { buildAgentLines } from './peerMsgs';
import { buildBoardList, type ProjectGroup } from '../util/boardList';
import { buildWaiting } from '../util/waiting';
import { waitingInputOf } from '../util/waitInput';

export function useBoardGroups(nowMs: () => number | undefined): ComputedRef<ProjectGroup[]> {
  return computed(() =>
    buildBoardList({
      projects: store.state.projects,
      sessions: Object.values(store.state.sessions.items),
      pending: store.state.sessions.pending,
      agentLines: buildAgentLines(store.state.peerMsgs.items, store.state.peerMsgs.agentSubs),
      offline: boardState.offline,
      hiddenLines: boardState.layout.hidden_lines,
      home: store.state.home,
      waiting: buildWaiting(waitingInputOf(store)),
      hiddenProjects: boardState.layout.hidden_projects,
      nowMs: nowMs() ?? Date.now(),
    }),
  );
}
