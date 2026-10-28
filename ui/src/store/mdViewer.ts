// Ф72-1 B: окно просмотра MD-файлов. Один хозяин на приложение (`MdViewerModal` в `MainApp`): открыть можно из
// любого места — `openMd(path, { sessionId?, projectId?, from? })`. Окно помнит стопку открытых файлов
// («‹ назад»); ссылка внутри файла кладёт в стопку новый (запрос с `from` = путь текущего файла).
import { reactive } from 'vue';

import * as api from '../api/client';

export interface MdCtx {
  sessionId?: number;
  projectId?: number;
  from?: string;
}

export interface MdEntry {
  /** Путь как его написали (до поиска). */
  request: string;
  ctx: MdCtx;
  status: 'loading' | 'ok' | 'not_found' | 'too_big' | 'bad_path' | 'error';
  /** Настоящий абсолютный путь найденного файла. */
  path: string | null;
  text: string;
  tried: string[];
  message: string | null;
}

export const mdViewer = reactive<{ stack: MdEntry[] }>({ stack: [] });

function fresh(request: string, ctx: MdCtx): MdEntry {
  return { request, ctx, status: 'loading', path: null, text: '', tried: [], message: null };
}

export async function loadEntry(entry: MdEntry): Promise<void> {
  entry.status = 'loading';
  try {
    const file = await api.getMdFile(entry.request, entry.ctx);
    const live = entry;
    live.path = file.path;
    live.text = file.text;
    live.status = 'ok';
  } catch (err) {
    const live = entry;
    if (err instanceof api.ApiError) {
      const detail = (err.body as { detail?: unknown } | null)?.detail;
      const obj = typeof detail === 'object' && detail !== null ? (detail as { code?: string; tried?: string[] }) : {};
      live.tried = Array.isArray(obj.tried) ? obj.tried : [];
      if (err.status === 404) live.status = 'not_found';
      else if (err.status === 413) live.status = 'too_big';
      else if (err.status === 422) live.status = 'bad_path';
      else live.status = 'error';
      live.message = live.status === 'error' ? 'Не получилось загрузить файл — попробуй ещё раз.' : null;
    } else {
      live.status = 'error';
      live.message = 'Не получилось загрузить файл — попробуй ещё раз.';
    }
  }
}

/** Открыть MD-файл: окно закрыто — начинает стопку, открыто — кладёт файл сверху (назад — к предыдущему). */
export function openMd(path: string, ctx: MdCtx = {}): void {
  const entry = reactive(fresh(path, { ...ctx }));
  mdViewer.stack.push(entry);
  void loadEntry(entry);
}

export function mdBack(): void {
  if (mdViewer.stack.length > 1) mdViewer.stack.pop();
}

export function mdClose(): void {
  mdViewer.stack = [];
}

export function mdReload(): void {
  const top = mdViewer.stack[mdViewer.stack.length - 1];
  if (top !== undefined) void loadEntry(top);
}
