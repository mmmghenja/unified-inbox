// Ф60-1C: состояние помощника на интервью (`docs/architecture.md` и «Контракт ручек A»).
// Одно интервью на весь хаб: запись, последние строки расшифровки, живость службы записи. Данные — из
// `GET /interviews/current` при открытии окна и кадров `event: interview` ОБЩЕГО потока `/sessions/live`
// (второго соединения нет; кадры принимает `Store.openLive` и передаёт сюда). Тексты строк — чужая речь.
import { reactive } from 'vue';

import * as api from '../api/client';
import type { Interview, InterviewFrame, InterviewLine } from '../api/types';

export const INTERVIEW_LINES_KEEP = 200;

export interface InterviewUi {
  interview: Interview | null;
  lines: InterviewLine[];
  listenerAlive: boolean;
  loaded: boolean;
  error: string | null;
}

export const interviewUi = reactive<InterviewUi>({
  interview: null,
  lines: [],
  listenerAlive: true,
  loaded: false,
  error: null,
});

let loading: Promise<void> | null = null;

export function resetInterview(): void {
  interviewUi.interview = null;
  interviewUi.lines = [];
  interviewUi.listenerAlive = true;
  interviewUi.loaded = false;
  interviewUi.error = null;
  loading = null;
}

/** Идёт ли интервью (`active` или `paused`) — только такое блокирует начало нового. */
export function interviewOpen(i: Interview | null): i is Interview {
  return i !== null && i.status !== 'ended';
}

function byTime(a: InterviewLine, b: InterviewLine): number {
  return a.start_ms - b.start_ms || a.id - b.id;
}

function mergeLines(base: InterviewLine[], extra: InterviewLine[]): InterviewLine[] {
  const seen = new Set(base.map((l) => l.id));
  const out = [...base];
  for (const l of extra) {
    if (!seen.has(l.id)) {
      seen.add(l.id);
      out.push(l);
    }
  }
  out.sort(byTime);
  return out.length > INTERVIEW_LINES_KEEP ? out.slice(out.length - INTERVIEW_LINES_KEEP) : out;
}

/** Перечитать `GET /interviews/current`. Параллельные вызовы сливаются в один. */
export function loadInterviewCurrent(): Promise<void> {
  if (loading !== null) return loading;
  loading = (async () => {
    try {
      const cur = await api.getInterviewCurrent();
      interviewUi.interview = cur.interview ?? null;
      interviewUi.lines = Array.isArray(cur.lines) ? [...cur.lines].sort(byTime) : [];
      interviewUi.listenerAlive = cur.listener_alive !== false;
      interviewUi.error = null;
      interviewUi.loaded = true;
    } catch (err) {
      console.error('интервью: текущее не прочиталось', err);
      interviewUi.error = 'Интервью не прочиталось — попробуй ещё раз.';
    } finally {
      loading = null;
    }
  })();
  return loading;
}

/** Кадр потока: `data` — JSON одного из трёх видов. Неразборчивое молча пропускаем. */
export function applyInterviewFrame(raw: unknown): void {
  let data: unknown = raw;
  if (typeof raw === 'string') {
    try {
      data = JSON.parse(raw);
    } catch {
      return;
    }
  }
  if (data === null || typeof data !== 'object') return;
  const frame = data as InterviewFrame;
  if (frame.type === 'resync') {
    void resync();
    return;
  }
  if (frame.type === 'state' && frame.interview !== undefined && frame.interview !== null) {
    const prev = interviewUi.interview;
    if (prev === null || prev.id !== frame.interview.id) interviewUi.lines = [];
    interviewUi.interview = frame.interview;
    interviewUi.listenerAlive = frame.listener_alive !== false;
    interviewUi.loaded = true;
    return;
  }
  if (frame.type === 'line' && frame.line !== undefined && frame.line !== null) {
    const cur = interviewUi.interview;
    if (cur === null || cur.id !== frame.interview_id) return;
    interviewUi.lines = mergeLines(interviewUi.lines, [frame.line]);
  }
}

/** `resync`: часть кадров потеряна — запись и последние строки перечитываются, строки после последнего id добираются. */
async function resync(): Promise<void> {
  const lastId = interviewUi.lines.reduce((m, l) => Math.max(m, l.id), 0);
  await loadInterviewCurrent();
  const cur = interviewUi.interview;
  if (cur === null) return;
  try {
    const more = await api.getInterviewLines(cur.id, lastId);
    if (Array.isArray(more.lines) && more.lines.length > 0) interviewUi.lines = mergeLines(interviewUi.lines, more.lines);
  } catch (err) {
    console.error('интервью: строки после resync не добрались', err);
  }
}

export type InterviewActionResult = { ok: true; queued?: boolean } | { ok: false; message: string };

function failure(err: unknown): InterviewActionResult {
  if (err instanceof api.ApiError) return { ok: false, message: err.message };
  console.error('интервью: действие не прошло', err);
  return { ok: false, message: 'Не получилось — попробуй ещё раз.' };
}

export async function startInterview(sessionId: number, mode: 'live' | 'button', lang: 'ru-RU' | 'en-US'): Promise<InterviewActionResult> {
  try {
    const { interview } = await api.startInterview({ session_id: sessionId, mode, them_lang: lang });
    interviewUi.interview = interview;
    interviewUi.lines = [];
    interviewUi.loaded = true;
    return { ok: true };
  } catch (err) {
    // 409 «уже идёт интервью в сессии N» — номер сессии берём у ядра, форма покажет ссылку.
    await loadInterviewCurrent();
    return failure(err);
  }
}

/** «Подскажи»: `queued: true` — подсказка уже запрошена, второй реплики нет. */
export async function hintInterview(id: number): Promise<InterviewActionResult> {
  try {
    const res = await api.interviewHint(id);
    interviewUi.interview = res.interview;
    return { ok: true, queued: res.queued === true };
  } catch (err) {
    return failure(err);
  }
}

export async function pauseInterview(id: number): Promise<InterviewActionResult> {
  try {
    interviewUi.interview = (await api.interviewPause(id)).interview;
    return { ok: true };
  } catch (err) {
    return failure(err);
  }
}

export async function resumeInterview(id: number): Promise<InterviewActionResult> {
  try {
    interviewUi.interview = (await api.interviewResume(id)).interview;
    return { ok: true };
  } catch (err) {
    return failure(err);
  }
}

export async function endInterview(id: number): Promise<InterviewActionResult> {
  try {
    interviewUi.interview = (await api.interviewEnd(id)).interview;
    return { ok: true };
  } catch (err) {
    return failure(err);
  }
}
