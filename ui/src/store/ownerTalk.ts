// Ф58-1 B3: лента разговора владельца с его агентом (`/owner_channel/talk`). Новые снизу; «ещё» — по `before`. Снимок очереди
// несёт `talk_unread`/`talk_open` — при их изменении окно перечитывает ленту. Старое ядро (404) — лента пуста.
import { reactive } from 'vue';

import * as api from '../api/client';
import type { SessionFileDraft, OwnerTalkBranch, OwnerTalkMsg } from '../api/types';

const PAGE = 50;

/** Ветка разговора: id сессии хаба или `null` — «Общее». */
export type TalkBranch = number | null;

export const talkState = reactive<{
  branch: TalkBranch;
  branches: OwnerTalkBranch[];
  items: OwnerTalkMsg[];
  loaded: boolean;
  olderDone: boolean;
  gone: boolean;
  error: string | null;
}>({ branch: null, branches: [], items: [], loaded: false, olderDone: false, gone: false, error: null });

export function resetTalk(): void {
  talkState.branch = null;
  talkState.branches = [];
  talkState.items = [];
  talkState.loaded = false;
  talkState.olderDone = false;
  talkState.gone = false;
  talkState.error = null;
}

function inBranch(m: OwnerTalkMsg, branch: TalkBranch): boolean {
  return (m.session_id ?? null) === branch;
}

/** Выбрать ветку: лента сбрасывается и читается заново (`loadTalk`). */
export function selectBranch(branch: TalkBranch): void {
  if (talkState.branch === branch) return;
  talkState.branch = branch;
  talkState.items = [];
  talkState.loaded = false;
  talkState.olderDone = false;
}

function merge(list: OwnerTalkMsg[]): void {
  const byId = new Map(talkState.items.map((m) => [m.id, m]));
  for (const m of list) if (inBranch(m, talkState.branch)) byId.set(m.id, m);
  talkState.items = [...byId.values()].sort((a, b) => a.id - b.id);
}

/** Список веток; «Общее» есть всегда (старое ядро без ручки — только оно). */
export async function loadBranches(): Promise<void> {
  let list: OwnerTalkBranch[] = [];
  try {
    const res = await api.getOwnerTalkBranches();
    list = Array.isArray(res?.items) ? res.items : [];
  } catch {
    list = [];
  }
  if (!list.some((b) => b.session_id === null)) {
    list = [{ session_id: null, title: 'Общее', project_id: null, role: null, last_id: 0, last_ts: 0, unread: 0 }, ...list];
  }
  talkState.branches = list;
}

/** Порядок списка: «Общее» первой, затем с непрочитанным, затем по времени. */
export function sortedBranches(list: OwnerTalkBranch[]): OwnerTalkBranch[] {
  const general = list.filter((b) => b.session_id === null);
  const rest = list
    .filter((b) => b.session_id !== null)
    .sort((a, b) => (b.unread > 0 ? 1 : 0) - (a.unread > 0 ? 1 : 0) || b.last_ts - a.last_ts);
  return [...general, ...rest];
}

export function branchTitle(b: OwnerTalkBranch): string {
  return b.session_id === null ? 'Общее' : b.title;
}

export async function loadTalk(): Promise<void> {
  const branch = talkState.branch;
  try {
    const page = await api.getOwnerTalk({ limit: PAGE, session_id: branch });
    if (branch !== talkState.branch) return;
    const list = Array.isArray(page?.items) ? page.items : [];
    merge(list);
    if (!talkState.loaded) talkState.olderDone = page?.has_more !== true;
    talkState.loaded = true;
    talkState.error = null;
  } catch (err) {
    if (err instanceof api.ApiError && err.status === 404) {
      talkState.gone = true;
      talkState.loaded = true;
    } else talkState.error = 'Разговор не загрузился — повторю при следующем обновлении.';
  }
}

export async function loadOlderTalk(): Promise<void> {
  const first = talkState.items[0];
  if (first === undefined || talkState.olderDone) return;
  try {
    const page = await api.getOwnerTalk({ before: first.id, limit: PAGE, session_id: talkState.branch });
    const list = Array.isArray(page?.items) ? page.items : [];
    merge(list);
    if (page?.has_more !== true) talkState.olderDone = true;
  } catch {
    talkState.error = 'Ещё не загрузилось.';
  }
}

export async function sendTalk(
  text: string,
  ref: string | null,
  files: string[] = [],
): Promise<{ ok: true } | { ok: false; message: string }> {
  try {
    const session_id = talkState.branch;
    const body = {
      text,
      session_id,
      ...(ref === null ? {} : { ref }),
      ...(files.length > 0 ? { files } : {}),
    };
    const msg = await api.postOwnerTalk(body);
    if (msg !== null && typeof msg === 'object' && typeof msg.id === 'number') merge([msg]);
    // Ушли — чипы снимаются (файлы ядро уже приложило); не ушли — остаются.
    if (files.length > 0) talkFiles.list = talkFiles.list.filter((f) => f.fileId === null || !files.includes(f.fileId));
    return { ok: true };
  } catch (err) {
    if (err instanceof api.ApiError && (err.status === 422 || err.status === 409)) return { ok: false, message: err.message };
    return { ok: false, message: 'Не отправилось — проверь связь и повтори.' };
  }
}

/** Непрочитанные реплики агента в загруженной ленте. */
export function unreadTalk(): OwnerTalkMsg[] {
  return talkState.items.filter((m) => m.author === 'agent' && m.read_ts === null);
}

/** Отметить прочитанным всё до последней реплики агента. */
export async function markTalkRead(): Promise<void> {
  const unread = unreadTalk();
  if (unread.length === 0) return;
  const upto = Math.max(...unread.map((m) => m.id));
  try {
    await api.markOwnerTalkRead(upto, talkState.branch);
    const b = talkState.branches.find((x) => x.session_id === talkState.branch);
    if (b !== undefined) b.unread = 0;
    const now = Date.now();
    for (const m of talkState.items) if (m.author === 'agent' && m.id <= upto && m.read_ts === null) m.read_ts = now;
  } catch (err) {
    console.error('разговор с агентом: отметка прочитанного не прошла', err);
  }
}

// --- 23.10: файлы к реплике ---------------------------

export const TALK_FILES_MAX = 10;

/** Приложенное к следующей реплике — чипы над кнопками (живёт только в странице). */
export const talkFiles = reactive<{ list: SessionFileDraft[] }>({ list: [] });

let fileSeq = 0;
const uploads = new Map<number, AbortController>();

function patchFile(localId: number, patch: Partial<SessionFileDraft>): boolean {
  const idx = talkFiles.list.findIndex((f) => f.localId === localId);
  if (idx === -1) return false;
  talkFiles.list[idx] = { ...talkFiles.list[idx]!, ...patch };
  return true;
}

async function upload(localId: number, file: File, name: string, controller: AbortController): Promise<void> {
  try {
    const result = await api.uploadTalkFile(file, name, controller.signal);
    if (!patchFile(localId, { fileId: result.id, name: result.name, size: result.size, kind: result.kind, status: 'ready' })) {
      // Убрали крестиком, пока ответ был в пути, — файл ядру не нужен.
      void api.deleteTalkFile(result.id).catch(() => undefined);
    }
  } catch (err) {
    let message = 'не загрузился';
    if (err instanceof api.ApiError && err.status === 413) message = 'файл больше 20 МБ';
    else if (err instanceof api.ApiError && (err.status === 409 || err.status === 422)) message = err.message;
    else if (!controller.signal.aborted) console.error('разговор: файл не загрузился', err);
    patchFile(localId, { status: 'error', error: message });
  } finally {
    uploads.delete(localId);
  }
}

/** Добавить файлы к следующей реплике и грузить их в ядро. Больше `TALK_FILES_MAX` не берём — пояснение строкой. */
export function attachTalkFiles(files: File[]): string | null {
  let note: string | null = null;
  for (const file of files) {
    if (talkFiles.list.length >= TALK_FILES_MAX) {
      note = `Не больше ${TALK_FILES_MAX} файлов в одной реплике.`;
      break;
    }
    const localId = ++fileSeq;
    const name = file.name || 'file';
    talkFiles.list = [...talkFiles.list, { localId, name, size: file.size, kind: null, status: 'uploading', fileId: null, error: null }];
    const controller = new AbortController();
    uploads.set(localId, controller);
    void upload(localId, file, name, controller);
  }
  return note;
}

/** «×» на чипе — отменить загрузку или убрать загруженный файл; с чипа снимается в любом случае. */
export async function removeTalkFile(localId: number): Promise<void> {
  const item = talkFiles.list.find((f) => f.localId === localId);
  if (item === undefined) return;
  talkFiles.list = talkFiles.list.filter((f) => f.localId !== localId);
  uploads.get(localId)?.abort();
  uploads.delete(localId);
  if (item.fileId !== null) {
    try {
      await api.deleteTalkFile(item.fileId);
    } catch (err) {
      console.error('разговор: файл не удалился (ядро уберёт само через сутки)', err);
    }
  }
}
