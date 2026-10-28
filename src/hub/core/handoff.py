"""«Новая с передачей дел» (Ф41-7б, docs/architecture.md).

Тяжёлую остывшую сессию хаба дешевле заменить новой, чем поднимать; так же меняется движок. В ОДНОЙ
транзакции: прежняя сессия закрывается (`close_reason: "handoff"`), на той же линии роли создаётся новая
(`created_by: "handoff"`), ей кладётся первая реплика владельца — текст передачи дел, за ней — снятые ждущие
реплики прежней в прежнем порядке, с их файлами.

Текст передачи дел собирает ядро из ленты прежней сессии (реплики владельца и итоговые тексты сессии), без вызова
модели. Все функции записи — только внутри `Db.write()`, время — параметром `now_ms`."""

from datetime import datetime
from pathlib import Path

from hub.engine import registry as engine_registry

from . import engine_fallback, held_work, transcript
from . import held as held_mod
from . import hub_sessions as hub_sessions_mod
from . import interview as interview_mod
from .audit import write_audit
from .hub_sessions import SessionError

CLOSE_REASON = "handoff"
TAIL_MAX = 12_000  # «последние реплики» целиком
ITEM_MAX = 1_500  # одна реплика; середина длинной вырезается
CUT_MARK = " … "
NOTE_SAY_MOVED = "не отправлено: передано новой сессии"
ENGINE_NAMES = engine_registry.LABELS  # Ф74-1: подписи — из профиля движка
OPEN_MAX = 20
SCAN_BATCH = 200


def _hhmm(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M")


def _stamp(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%d.%m.%Y %H:%M")


def clip_middle(text: str, limit: int = ITEM_MAX) -> str:
    """Не длиннее `limit`: у длинного вырезается середина с пометкой «…» (начало и конец остаются)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    keep = limit - len(CUT_MARK)
    head = keep // 2 + keep % 2
    return text[:head] + CUT_MARK + text[len(text) - (keep - head) :]


def _say_text(summary: str) -> str:
    """Событие `say` пишется как «Владелец (next): текст» — в выжимку идёт сам текст."""
    head, sep, tail = summary.partition("): ")
    return tail if sep and head.startswith("владелец (") else summary


def tail_lines(
    conn, rec: held_mod.HubSession, skip_says: frozenset[str] = frozenset()
) -> tuple[list[str], int, int | None]:
    """2.2: реплики владельца и итоговые тексты сессии с конца ленты, пока сумма не превысит `TAIL_MAX`; каждая
    — не длиннее `ITEM_MAX`. Сигналы, шаги инструментов, карточки и служебные строки `say` (снятые реплики —
    у них есть причина, а не текст владельца) в выжимку не идут; не идут и реплики, ещё ждущие в очереди
    (`skip_says` — их id): они придут новой сессии следом своими репликами. Возвращает строки от старых
    к новым, число сжатий контекста и время последнего."""
    lines: list[str] = []
    total = 0
    compacts = 0
    last_compact: int | None = None
    before: int | None = None
    full = False
    while True:
        batch = held_work.list_events(conn, rec, after=None, limit=SCAN_BATCH, before=before or 2**62)
        if not batch:
            break
        for event in reversed(batch):
            if event["kind"] == "compact":
                compacts += 1
                last_compact = last_compact or event["ts"]
                continue
            if full or event["kind"] not in ("say", "text"):
                continue
            if event["kind"] == "say":
                if not event["summary"].startswith("владелец (") or event["ref"] in skip_says:
                    continue
                who, text = "владелец", _say_text(event["summary"])
            else:
                who, text = "сессия", event["summary"]
            text = clip_middle(text)
            if not text:
                continue
            line = f"[{who} {_hhmm(event['ts'])}] {text}"
            if lines and total + len(line) > TAIL_MAX:
                full = True
                continue
            lines.append(line)
            total += len(line)
        before = batch[0]["id"]
        if len(batch) < SCAN_BATCH:
            break
    lines.reverse()
    return lines, compacts, last_compact


def codex_record(thread_id: str, root: Path | None = None) -> Path | None:
    """Ф74-5: запись нити Codex — `~/.codex/sessions/<год>/<мес>/<день>/rollout-<время>-<id нити>.jsonl`
    (проба 21.10); нет — `None`."""
    if not thread_id or "/" in thread_id or "*" in thread_id:
        return None
    base = root if root is not None else Path.home() / ".codex" / "sessions"
    try:
        found = sorted(base.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    except OSError:
        return None
    return found[-1] if found else None


def cursor_record(chat_id: str, root: Path | None = None) -> Path | None:
    """Ф74-8: запись разговора Cursor — `~/.cursor/projects/<слаг>/agent-transcripts/<chatId>/<chatId>.jsonl`
    (слаг папки не вычисляется — поиск по chatId, как у хоста, Ф71-1 П9); нет — `None`."""
    if not chat_id or any(c in chat_id for c in "/*?[]") or chat_id in (".", ".."):
        return None
    base = root if root is not None else Path.home() / ".cursor" / "projects"
    try:
        found = sorted(base.glob(f"*/agent-transcripts/{chat_id}/{chat_id}.jsonl"))
    except OSError:
        return None
    return found[-1] if found else None


def build_text(
    conn,
    rec: held_mod.HubSession,
    *,
    project_name: str,
    requests: list[dict],
    now_ms: int,
    claude_root: Path | None = None,
    skip_says: frozenset[str] = frozenset(),
) -> str:
    """2.2: текст передачи дел — первая реплика новой сессии. Чужого текста (сообщений людей и агентов) в
    событиях `say`/`text` нет — рамка данных не нужна."""
    role = f"{rec.role} {rec.role_no}" if rec.role else "без роли"
    engine = ENGINE_NAMES.get(rec.engine, rec.engine)
    parts = [
        f"Передача дел. Ты продолжаешь работу прежней сессии хаба: проект «{project_name}», роль «{role}», "
        f"движок {engine}, её разговор закрыт {_stamp(now_ms)}. Прежний разговор тебе не виден — ниже его "
        "конец."
    ]
    path = None
    if rec.engine == "claude" and rec.engine_session_id:
        path = transcript.find(rec.engine_session_id, claude_root)
    elif rec.engine == "codex" and rec.engine_session_id:
        path = codex_record(rec.engine_session_id)
    elif rec.engine == "cursor" and rec.engine_session_id:
        path = cursor_record(rec.engine_session_id)
    if path is not None:
        parts.append(
            f"Полная запись прежнего разговора: {path} (читай по мере надобности, целиком не загружай)."
        )
    lines, compacts, last_compact = tail_lines(conn, rec, skip_says)
    if compacts and last_compact is not None:
        parts.append(f"Контекст сжимался {compacts} раз, последний — {_stamp(last_compact)}.")
    if lines:
        parts.append("Последние реплики (от старых к новым):")
        parts.extend(lines)
    else:
        parts.append("Реплик в прежнем разговоре не было.")
    if requests:
        parts.append("Открытые вопросы на момент передачи:")
        for request in requests[:OPEN_MAX]:
            tool = held_work._clip(str(request.get("tool") or "?"), 100)  # noqa: SLF001
            reason = " ".join(str(request.get("reason") or "").split())
            parts.append(f"- {tool}: {clip_middle(reason, 300)}" if reason else f"- {tool}")
    else:
        parts.append("Открытые вопросы на момент передачи: нет.")
    parts.append(
        "Коротко подтверди, что принял дела (что понял про текущую задачу и что осталось), и жди указаний — "
        "если ниже нет новых реплик владельца."
    )
    return "\n".join(parts)[: held_work.SAY_MAX]


def _take_files(conn, store: str, rec: held_mod.HubSession, say_id: int) -> list[str]:
    """Файлы ждущей реплики выводятся из прежней сессии (строки остаются — счёт ссылок на байты цел, и
    закрытие, стирающее файлы сессии, их не видит): «ничья» отрицательная сессия до выдачи новой."""
    ids = [
        row[0]
        for row in conn.execute(
            f"SELECT id FROM {store}.held_file WHERE session_id = ? AND say_id = ? "  # noqa: S608
            "AND purged_ts IS NULL ORDER BY created_ts, id",
            (rec.id, say_id),
        )
    ]
    if ids:
        conn.execute(
            f"UPDATE {store}.held_file SET session_id = ?, say_id = NULL, released_ts = NULL "  # noqa: S608
            "WHERE session_id = ? AND say_id = ? AND purged_ts IS NULL",
            (-rec.id, rec.id, say_id),
        )
    return ids


def _give_files(conn, store: str, old_id: int, new_id: int) -> None:
    conn.execute(
        f"UPDATE {store}.held_file SET session_id = ? WHERE session_id = ?",  # noqa: S608
        (new_id, -old_id),
    )


def handoff(
    conn,
    hub_session_id: int,
    *,
    engine: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    subdir: str | None = None,
    given: frozenset[str] = frozenset(),
    now_ms: int,
    audit: dict,
    waiting=None,
    claude_root: Path | None = None,
) -> tuple[held_mod.HubSession, held_mod.HubSession]:
    """2.1: `(новая, прежняя закрытая)`. `given` — какие поля тела заданы (`model: null` — умолчание движка,
    нет ключа — как у прежней). Ф50-7 (П5): `subdir` в `given` — папка новой сессии (`null` или пустая
    строка — корень проекта), нет ключа — папка прежней; проверки папки — в `create` (422, ничего не
    меняется). Отказы: 404 — сессии нет; 409 — закрыта или идёт ход; отказы
    `hub_sessions.create` (потолок сессий, проект без папки) — как есть, ничего не меняется."""
    rec = held_mod.get(conn, hub_session_id)
    if rec is None:
        raise SessionError(404, "not found")
    if rec.closed_ts is not None:
        raise SessionError(409, "сессия хаба закрыта")
    if rec.state == "running":
        raise SessionError(409, "идёт ход — дождись конца хода или прерви")
    interview_mod.check_free(conn, rec.id)  # Ф60-1 (П8): идёт интервью — сначала «Закончить»
    project =conn.execute("SELECT name FROM main.project WHERE id = ?", (rec.project_id,)).fetchone()
    requests = held_work._requests_where(conn, rec, held_work._UNDELIVERED)  # noqa: SLF001
    says = held_work.queued_says(conn, rec)
    text = build_text(
        conn,
        rec,
        skip_says=frozenset(str(item["id"]) for item in says),
        project_name=(project[0] if project else None) or "без названия",
        requests=requests,
        now_ms=now_ms,
        claude_root=claude_root,
    )
    # Ждущие реплики — запомнить и снять; их файлы уходят из прежней сессии ДО закрытия (оно стирает байты
    # файлов сессии сразу).
    moved = [(item, _take_files(conn, rec.store, rec, item["id"])) for item in says] if rec.store else []
    if says:
        held_work._drop_queued_says(conn, rec, now_ms, NOTE_SAY_MOVED)  # noqa: SLF001
    closed, _ = hub_sessions_mod.close(conn, rec.id, now_ms=now_ms, audit=audit, reason=CLOSE_REASON)

    new = hub_sessions_mod.create(
        conn,
        project_id=rec.project_id,
        engine=engine or rec.engine,
        role=rec.role,
        number=rec.role_no if rec.role_key is not None else None,
        model=model if "model" in given else (rec.model if (engine or rec.engine) == rec.engine else None),
        effort=effort
        if "effort" in given
        else (rec.effort if (engine or rec.engine) == rec.engine else None),
        title=rec.title,
        created_by="handoff",
        now_ms=now_ms,
        audit=audit,
        waiting=waiting,
        mode=mode or rec.mode,
        # Ф50-7 (П5): аргументом `create`, а не последующим UPDATE — проверки Р3 и П4 проходят.
        subdir=subdir if "subdir" in given else rec.subdir,
    )
    # `want_sub` и `autosend_ok` — от прежней как есть (фильтр уже проверен при записи). Ф48-2 (D): и
    # настройка сжатия — свой порог и выключатель авто-сжатия. Ф48-3: и файл роли.
    # Ф83-1 (П8): и запасной движок сессии, и пометка «на запасном» (передача на движок `main` пометки её
    # снимает — это возврат на основной).
    conn.execute(
        "UPDATE main.hub_session SET want_sub_json = ?, autosend_ok = ?, compact_tokens = ?, "
        "compact_auto = ?, role_file = ?, fallback_json = ?, on_fallback_json = ? WHERE id = ?",
        (
            rec.want_sub_json,
            rec.autosend_ok,
            rec.compact_tokens,
            rec.compact_auto,
            rec.role_file,
            rec.fallback_json,
            engine_fallback.carry_marker(rec, new.engine),
            new.id,
        ),
    )
    # Ф48-2 (A): прерванные реплики переезжают прерванными и держат несрочные за собой — текст передачи
    # дел тогда идёт срочным (у спящей `now` = `next`, прерывать нечего), иначе новая сессия не начнёт.
    held_back = any(item["interrupted_ts"] is not None for item, _files in moved)
    held_work.say(
        conn, new.id, text=text, priority="now" if held_back else "next", now_ms=now_ms, audit=audit
    )
    new_rec = held_mod.get(conn, new.id)
    assert new_rec is not None  # noqa: S101 — строка этой транзакции
    if moved:
        _give_files(conn, held_work._store(conn, new_rec), rec.id, new.id)  # noqa: SLF001
    for item, file_ids in moved:
        say_id = held_work.say(
            conn,
            new.id,
            text=item["text"],
            priority=item["priority"],
            now_ms=now_ms,
            audit=audit,
            files=file_ids or None,
            # Ф60-1 (П1): неподанная подача интервью (оно уже окончено — иначе передача дел 409) остаётся
            # подачей интервью: не ответ владельца и при доставке без текста в ленте.
            interview_id=item.get("interview_id"),
            # Ф86-1 (П3): перенесённая реплика сохраняет «набрал владелец» исходной; сам текст передачи — нет.
            typed=bool(item.get("typed")),
        )
        if item["interrupted_ts"] is not None:
            held_work.mark_interrupted(conn, new_rec, say_id, item["interrupted_ts"])
    write_audit(
        conn,
        now_ms=now_ms,
        action="hub_session_handoff",
        result="ok",
        detail={
            "hub_session_id": rec.id,
            "new_session_id": new.id,
            "project_id": rec.project_id,
            "engine": new.engine,
            "says": len(moved),
            "requests": len(requests),
            "text_len": len(text),
        },
        **audit,
    )
    result = held_mod.get(conn, new.id)
    assert result is not None  # noqa: S101
    return result, closed


def successor(conn, rec: held_mod.HubSession) -> int | None:
    """Кому переданы дела закрытой сессии (`close_reason == "handoff"`) — по журналу, без своей колонки."""
    if rec.close_reason != CLOSE_REASON:
        return None
    row = conn.execute(
        "SELECT json_extract(detail_json, '$.new_session_id') FROM main.audit "
        "WHERE action = 'hub_session_handoff' AND json_extract(detail_json, '$.hub_session_id') = ? "
        "ORDER BY id DESC LIMIT 1",
        (rec.id,),
    ).fetchone()
    return row[0] if row is not None and isinstance(row[0], int) else None
