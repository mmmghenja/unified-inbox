// Ф41-5 (интерфейс): заявки агентов — выбор видимых и тексты карточки. Чистые функции без Vue. Все поля заявки
// (`who`, `reason`, `note`, `payload`) — чужой ввод: сюда они попадают только строками для текстовой интерполяции.
import type { AgentRequest } from '../api/types';
import { sessionEngineLabel } from './engineProfiles';

/** Видимые карточки — только ждущие решения (исход виден в самой беседе/сессии, а принятое автопилотом — в ответе
 * инструмента); по порядку подачи. */
export function visibleAgentRequests(requests: AgentRequest[]): AgentRequest[] {
  return requests.filter((r) => r.status === 'pending').sort((a, b) => a.id - b.id);
}

/** «<кто> просит перенести беседу «…» в «<проект>»». */
export function moveHeadline(request: AgentRequest, projectName: string): string {
  const who = request.who?.trim() || 'агент';
  const title = request.payload?.channel_title?.trim() || `#${request.payload?.channel_id ?? '?'}`;
  return `${who} просит перенести беседу «${title}» в «${projectName}»`;
}

const MODE_LABEL: Record<string, string> = { manual: 'ручной', auto: 'авто' };

/** «<кто> просит напарника: «роль», движок, режим — <задача>». */
export function partnerHeadline(request: AgentRequest): string {
  const who = request.who?.trim() || 'агент';
  const p = request.payload;
  const parts: string[] = [`«${p?.role?.trim() || 'без роли'}»`];
  if (p?.engine) parts.push(sessionEngineLabel(p.engine));
  if (p?.mode) parts.push(MODE_LABEL[p.mode] ?? p.mode);
  const task = p?.task?.trim();
  return `${who} просит напарника: ${parts.join(', ')}${task ? ` — ${task}` : ''}`;
}
