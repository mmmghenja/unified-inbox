"""Разговор владельца со своим агентом вне карточек (Ф58-1, docs/architecture.md).

Лента `main.owner_talk`: реплики владельца (`author='owner_channel'`, кука интерфейса) и агента (`author='agent'`,
bearer
ровно со скоупом `human`). Реплику владельца агент «обрабатывает» (`handled_ts`: ответил или отказался), реплику
агента владелец «читает» (`read_ts`). Лента общая, не по проектам: агент видит её целиком.

Любая запись двигает глобальный `seq` — просыпаются длинные опросы `/owner_channel/changes` агента и интерфейса;
запись без изменений (повтор `handled`, `read` без новых строк) `seq` не двигает. Текст реплик в аудит не идёт
(кто, id, длина). Запись — только внутри `Db.write()`, время — параметром `now_ms`; SQL разговора — только
здесь.

Ф61-1 (docs/architecture.md и поправки П2, П3): реплика агента может быть **вопросом**
владельцу (`ask`, срок ожидания `ask_until_ts`). Вопрос открыт, пока агент его не закрыл (`closed`), срок не вышел
и нет реплики владельца с `reply_to` на него. Бот Telegram видит вопросы (`asks`) и пишет ответ владельца (`bot_reply`,
`via = 'bot'`); повтор с тем же номером сообщения Telegram второй реплики не даёт.

Ф66-2 (docs/architecture.md): лента делится на ветки — `session_id` реплики (id
сессии хаба; NULL — «Общее»). Ветка новой реплики: явный `session_id` -> ветка реплики `reply_to` -> сессия
карточки `ref` -> «Общее». Пересказ ветки для модели агента — `main.owner_talk_branch` («Общее» —
`session_id = 0`): пишет только агент, `summary_upto_id` не назад и не дальше последней реплики ветки.

23.10: реплика владельца может нести файлы (`files` — id, загруженные `POST /owner_channel/talk/files`);
тогда текст может быть пустым. В строках ленты — `files: [{id, name, mime, size, kind, gone}]`.

Ф94-1 (docs/architecture.md): реплика агента может быть **сообщением** владельцу (`tell`) — бот
шлёт её в Telegram один раз (`tells`), ответа не ждёт; в ленте «Мой агент» — обычная реплика агента."""

from . import held as held_mod
from . import held_files
from . import owner_session as session_mod
from .audit import write_audit
from .db import next_seq
from .owner_queue import OwnerError

TEXT_MAX = 8000
REF_MAX = 64
LIST_MAX = 200
LIST_DEFAULT = 50
AUTHORS = ("owner_channel", "agent")
# Ф61-1 П2: срок ожидания вопроса агента, секунды.
ASK_TTL_MIN = 60
ASK_TTL_MAX = 7200
ASK_TTL_DEFAULT = 1800
ASK_TTL_NONE = 0  # Ф85-1 (A.1): вопрос агента без срока
ASK_WINDOW_MS = 24 * 60 * 60_000  # вопросы для бота — не старше суток (П1: старше — подрезается)
ASKS_MAX = 200
OUTCOMES = ("answered", "gave_up")
STATES = ("open", "answered", "closed", "expired")
REF_TITLE_MAX = 200
SUMMARY_MAX = 6000  # Ф66-2: пересказ ветки
GENERAL_TITLE = "Общее"


class _Mark:
    """Ф66-2: метки-параметры ветки (не значения `session_id`)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


ALL = _Mark("ALL")  # ветка-параметр: `ALL` — вся лента, `None` — «Общее», `int` — сессия хаба
UNSET = _Mark("UNSET")  # `session_id` в теле не передан — ветку выбирает ядро (A.2)

_COLUMNS = (
    "id, ts, author, text, ref, reply_to, handled_ts, read_ts, "
    "ask, ask_until_ts, closed_ts, closed_outcome, via, session_id, progress, progress_ts"
)
PROGRESS_MAX = 2000


class AskNotOpen(OwnerError):
    """Ф61-1 П2: ответ из бота на вопрос, который уже не открыт; `state` — его состояние."""

    def __init__(self, state: str) -> None:
        super().__init__(409, f"ask_not_open: агент уже не ждёт ответа на этот вопрос ({state})")
        self.state = state


def _row(row) -> dict:
    return {
        "id": row[0],
        "ts": row[1],
        "author": row[2],
        "text": row[3],
        "ref": row[4],
        "reply_to": row[5],
        "handled_ts": row[6],
        "read_ts": row[7],
        "ask": bool(row[8]),
        "ask_until_ts": row[9],
        "closed_ts": row[10],
        "closed_outcome": row[11],
        "via": row[12],
        "session_id": row[13],
        "progress": row[14],
        "progress_ts": row[15],
    }


def _with_files(conn, items: list[dict]) -> list[dict]:
    files = held_files.of_talks(conn, [i["id"] for i in items if i["author"] == "owner_channel"])
    for item in items:
        item["files"] = files.get(item["id"], [])
    return items


def _get(conn, talk_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM main.owner_talk WHERE id = ?",  # noqa: S608
        (talk_id,),
    ).fetchone()
    return _with_files(conn, [_row(row)])[0] if row is not None else None


def _branch_where(branch) -> tuple[list[str], list]:
    """Условие ветки: `ALL` — без условия, `None` — «Общее», `int` — сессия хаба."""
    if branch is ALL:
        return [], []
    if branch is None:
        return ["session_id IS NULL"], []
    return ["session_id = ?"], [int(branch)]


def listing(conn, *, before: int | None = None, limit: int = LIST_DEFAULT, branch=ALL) -> dict:
    """Последние `limit` реплик ленты (до `before`, не включая), новые снизу: `{items, has_more}`. Ф66-2:
    `branch` — только реплики ветки (`None` — «Общее», `int` — сессия хаба; `ALL` — вся лента)."""
    limit = max(1, min(int(limit), LIST_MAX))
    where, params = _branch_where(branch)
    if before is not None:
        where.append("id < ?")
        params.append(before)
    cond = f"WHERE {' AND '.join(where)} " if where else ""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM main.owner_talk {cond}ORDER BY id DESC LIMIT ?",  # noqa: S608
        (*params, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    return {"items": _with_files(conn, [_row(r) for r in reversed(rows[:limit])]), "has_more": has_more}


def open_replies(conn) -> list[dict]:
    """Реплики владельца без `handled_ts`, по возрастанию id (не больше `LIST_MAX`)."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM main.owner_talk WHERE author = 'owner_channel' AND handled_ts IS NULL "  # noqa: S608
        "ORDER BY id LIMIT ?",
        (LIST_MAX,),
    ).fetchall()
    return _with_files(conn, [_row(r) for r in rows])


def counts(conn) -> dict:
    """Числа для блока `agent` снимка очереди: `talk_unread` — реплики агента без `read_ts`, `talk_open` —
    реплики владельца без `handled_ts`, `talk_last_id` — id последней реплики (0 — лента пуста), `talk_progress` —
    время последней строки хода у необработанных реплик (0 — нет; будит опрос окна). Владелец 16.10:
    непрочитанное веток закрытых сессий хаба не считается (их ветки и в списке не показываются)."""
    unread = conn.execute(
        "SELECT COUNT(*) FROM main.owner_talk WHERE author = 'agent' AND read_ts IS NULL "
        "AND (session_id IS NULL OR session_id NOT IN "
        "(SELECT id FROM main.hub_session WHERE closed_ts IS NOT NULL))"
    ).fetchone()[0]
    open_ = conn.execute(
        "SELECT COUNT(*) FROM main.owner_talk WHERE author = 'owner_channel' AND handled_ts IS NULL"
    ).fetchone()[0]
    last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM main.owner_talk").fetchone()[0]
    progress = conn.execute(
        "SELECT COALESCE(MAX(progress_ts), 0) FROM main.owner_talk "
        "WHERE author = 'owner_channel' AND handled_ts IS NULL"
    ).fetchone()[0]
    return {"talk_unread": unread, "talk_open": open_, "talk_last_id": last, "talk_progress": progress}


def _check(conn, text, ref, reply_to, *, has_files=False) -> tuple[str, str | None, int | None]:
    if not isinstance(text, str) or (not text.strip() and not has_files):
        raise OwnerError(422, "text: пустая реплика")
    if len(text) > TEXT_MAX:
        raise OwnerError(422, f"text: не длиннее {TEXT_MAX} символов")
    if ref is not None and (not isinstance(ref, str) or not ref.strip() or len(ref) > REF_MAX):
        raise OwnerError(422, f"ref: строка 1–{REF_MAX} символов")
    if reply_to is not None:
        if isinstance(reply_to, bool) or not isinstance(reply_to, int) or _get(conn, reply_to) is None:
            raise OwnerError(422, "reply_to: нет такой реплики")
    return text.strip(), (ref.strip() if ref is not None else None), reply_to


def _ask_ttl(ask, ask_ttl_s) -> int | None:
    """П2: срок ожидания вопроса, секунды (`None` — не вопрос). Ф85-1 (A.1): `0` — вопрос без срока
    (`ask_until_ts = NULL`): открыт, пока агент его не закрыл или владелец не ответил."""
    if not isinstance(ask, bool):
        raise OwnerError(422, "ask: true или false")
    if not ask:
        if ask_ttl_s is not None:
            raise OwnerError(422, "ask_ttl_s: только вместе с ask")
        return None
    if ask_ttl_s is None:
        return ASK_TTL_DEFAULT
    if (
        isinstance(ask_ttl_s, bool)
        or not isinstance(ask_ttl_s, int)
        or not (ask_ttl_s == ASK_TTL_NONE or ASK_TTL_MIN <= ask_ttl_s <= ASK_TTL_MAX)
    ):
        raise OwnerError(422, f"ask_ttl_s: 0 (без срока) или целое {ASK_TTL_MIN}–{ASK_TTL_MAX}")
    return ask_ttl_s


def check_session(conn, session_id, *, project_ids=None) -> int:
    """Ф66-2 (A.2): явная ветка — существующая сессия хаба (и закрытая); `project_ids` — скоуп токена
    (`None` — все проекты). Иначе 422."""
    if isinstance(session_id, bool) or not isinstance(session_id, int):
        raise OwnerError(422, "session_id: номер сессии хаба или null")
    rec = held_mod.get(conn, session_id)
    if rec is None or (project_ids is not None and rec.project_id not in project_ids):
        raise OwnerError(422, "session_id: нет такой сессии хаба")
    return rec.id


def _branch_of(conn, *, session_id, reply_to, ref, now_ms: int, project_ids) -> int | None:
    """Ф66-2 (A.2): ветка новой реплики — явный `session_id` (`None` — «Общее»), иначе ветка реплики
    `reply_to`, иначе сессия карточки `ref` в момент записи, иначе «Общее»."""
    if session_id is not UNSET:
        return None if session_id is None else check_session(conn, session_id, project_ids=project_ids)
    if reply_to is not None:
        row = conn.execute("SELECT session_id FROM main.owner_talk WHERE id = ?", (reply_to,)).fetchone()
        return row[0] if row is not None else None
    if ref is not None:
        return session_mod.session_of_ref(conn, ref, now_ms=now_ms)
    return None


def post(
    conn,
    *,
    author: str,
    text,
    ref=None,
    reply_to=None,
    ask=False,
    ask_ttl_s=None,
    session_id=UNSET,
    project_ids=None,
    files=None,
    tell=False,
    now_ms: int,
    audit: dict,
) -> dict:
    """Новая реплика ленты -> строка. `author` ставит маршрут по принципалу (кука — `owner_channel`, агент —
    `agent`). Ф61-1 П2: `ask` (вопрос владельцу со сроком `ask_ttl_s`; Ф85-1: `0` — без срока) — только у реплики
    агента. Ф66-2: ветка — `_branch_of` (`session_id` не передан — `UNSET`; `project_ids` — скоуп токена для
    явной ветки). 23.10: `files` — id файлов разговора, только у реплики владельца. Ф94-1: `tell` — сообщение
    агента владельцу для бота (не вопрос)."""
    if author not in AUTHORS:
        raise ValueError(f"author: {author!r}")
    if ask is not False and author != "agent":
        raise OwnerError(422, "ask: вопрос задаёт только агент")
    if tell is not False and (author != "agent" or ask is not False):
        raise OwnerError(422, "tell: сообщение владельцу — только реплика агента и не вопрос")
    ttl = _ask_ttl(ask, ask_ttl_s)
    if files and author != "owner_channel":
        raise OwnerError(422, "files: файлы прикладывает только владелец")
    picked = held_files.talk_pick(conn, files) if files else []
    text, ref, reply_to = _check(conn, text, ref, reply_to, has_files=bool(picked))
    branch = _branch_of(
        conn, session_id=session_id, reply_to=reply_to, ref=ref, now_ms=now_ms, project_ids=project_ids
    )
    until = now_ms + ttl * 1000 if ttl else None  # Ф85-1: `0` — без срока
    cur = conn.execute(
        "INSERT INTO main.owner_talk (ts, author, text, ref, reply_to, ask, ask_until_ts, session_id, tell) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_ms, author, text, ref, reply_to, 1 if ttl is not None else 0, until, branch, 1 if tell else 0),
    )
    talk_id = cur.lastrowid
    held_files.talk_attach(conn, picked, talk_id)
    next_seq(conn)
    detail = {"id": talk_id, "author": author, "len": len(text)}
    if picked:
        detail["files"] = len(picked)
    if ttl is not None:
        detail["ask"] = True
    if tell:
        detail["tell"] = True
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_talk",
        result="created",
        detail=detail,
        **audit,
    )
    return _get(conn, talk_id)


def mark_handled(conn, talk_id: int, *, now_ms: int) -> dict:
    """Агент обработал реплику владельца: `handled_ts`. Нет реплики — 404; реплика агента — 409; повтор — та же
    строка без изменений."""
    found = _get(conn, talk_id)
    if found is None:
        raise OwnerError(404, "not found")
    if found["author"] != "owner_channel":
        raise OwnerError(409, "это реплика агента — отметка «обработано» только у реплик владельца")
    if found["handled_ts"] is not None:
        return found
    conn.execute("UPDATE main.owner_talk SET handled_ts = ? WHERE id = ?", (now_ms, talk_id))
    next_seq(conn)
    return _get(conn, talk_id)


def set_progress(conn, talk_id: int, *, text, now_ms: int) -> dict:
    """Строка хода агента у реплики владельца (план, шаги, время): заменяет прежнюю, новой записи ленты нет. Нет
    реплики — 404; реплика агента — 409; уже обработана — 409 (агент перестаёт обновлять); пустой или длиннее
    `PROGRESS_MAX` текст — 422."""
    if not isinstance(text, str) or not text.strip() or len(text) > PROGRESS_MAX:
        raise OwnerError(422, f"text: 1–{PROGRESS_MAX} символов")
    found = _get(conn, talk_id)
    if found is None:
        raise OwnerError(404, "not found")
    if found["author"] != "owner_channel":
        raise OwnerError(409, "это реплика агента — строка хода только у реплик владельца")
    if found["handled_ts"] is not None:
        raise OwnerError(409, "реплика уже обработана")
    conn.execute(
        "UPDATE main.owner_talk SET progress = ?, progress_ts = ? WHERE id = ?",
        (text.strip(), now_ms, talk_id),
    )
    next_seq(conn)
    return _get(conn, talk_id)


def mark_read(conn, upto: int, *, now_ms: int, branch=ALL) -> dict:
    """Владелец прочёл реплики агента с `id ≤ upto`: `{read: N}` — сколько отмечено сейчас. Ф66-2: `branch` —
    только в этой ветке (`ALL` — во всей ленте)."""
    where, params = _branch_where(branch)
    cond = "".join(f" AND {w}" for w in where)
    cur = conn.execute(
        "UPDATE main.owner_talk SET read_ts = ? WHERE author = 'agent' AND read_ts IS NULL AND id <= ?"  # noqa: S608
        + cond,
        (now_ms, upto, *params),
    )
    if cur.rowcount:
        next_seq(conn)
    return {"read": cur.rowcount}


# --- вопросы агента и ответ из бота (Ф61-1, B1 и П1–П3) ----------------------------------------------------


def _answered_ids(conn, ids: list[int]) -> set[int]:
    """Вопросы из `ids`, на которые есть реплика владельца с `reply_to`."""
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT DISTINCT reply_to FROM main.owner_talk WHERE author = 'owner_channel' AND reply_to IN ({marks})",  # noqa: S608
        ids,
    ).fetchall()
    return {r[0] for r in rows}


def ask_state(row: dict, *, replied: bool, now_ms: int) -> str:
    """П2: состояние вопроса агента — `answered` (есть ответ с `reply_to` или агент закрыл с `answered`),
    `closed` (агент закрыл с `gave_up`), `expired` (срок вышел), иначе `open`."""
    if replied or row["closed_outcome"] == "answered":
        return "answered"
    if row["closed_outcome"] == "gave_up":
        return "closed"
    until = row["ask_until_ts"]
    if until is not None and until <= now_ms:
        return "expired"
    return "open"


def answer_of(conn, talk_id: int, *, now_ms: int) -> dict:
    """Ф85-1 (A.2): ответ владельца на вопрос агента `talk_id` -> `{id, state, answer}`; `answer` — последняя
    реплика владельца с `reply_to = talk_id` (форма строки ленты) или `None`. Отметка «обработано» на чтение не
    влияет. Нет реплики или она не вопрос агента — 404."""
    found = _get(conn, talk_id)
    if found is None or found["author"] != "agent" or not found["ask"]:
        raise OwnerError(404, "not found")
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM main.owner_talk WHERE author = 'owner_channel' AND reply_to = ? "  # noqa: S608
        "ORDER BY id DESC LIMIT 1",
        (talk_id,),
    ).fetchone()
    answer = _with_files(conn, [_row(row)])[0] if row is not None else None
    return {
        "id": talk_id,
        "state": ask_state(found, replied=answer is not None, now_ms=now_ms),
        "answer": answer,
    }


def clamp_since(since_ms: int, now_ms: int) -> int:
    """П1: отметка старше суток подрезается до «сейчас − 24 ч» (часы клиента и ядра расходятся)."""
    return max(int(since_ms), now_ms - ASK_WINDOW_MS)


def _hands(marks: dict, ref) -> dict:
    """Ф96-1 (B.1): поля рук реплики по её карточке — открытое поручение по ней (`errand_marks` очереди)."""
    mark = marks.get(ref) if ref else None
    if mark is None:
        return {"errand_id": None, "hands": None, "hands_ts": None}
    return {"errand_id": mark["errand_id"], "hands": mark["hands"], "hands_ts": mark["hands_ts"]}


def asks(conn, *, since_ms: int, now_ms: int, titles=None, errands=None) -> list[dict]:
    """Вопросы агента не старше `since_ms` (не раньше суток назад), последние `ASKS_MAX`, по возрастанию id:
    `[{id, ts, text, ref, ref_title, state}]`. `titles(refs) -> {ref: title}` — короткие названия карточек
    очереди (зовётся только при непустом наборе `ref`). Ф96-1 (B.1): `errands(refs) -> {ref: метка}`
    (`owner_queue.errand_marks`) — у строки ещё `errand_id`, `hands`, `hands_ts` открытого поручения по её
    карточке (нет поручения — `null`)."""
    since = clamp_since(since_ms, now_ms)
    rows = [
        _row(r)
        for r in conn.execute(
            f"SELECT {_COLUMNS} FROM main.owner_talk WHERE ask = 1 AND author = 'agent' AND ts >= ? "  # noqa: S608
            "ORDER BY id DESC LIMIT ?",
            (since, ASKS_MAX),
        ).fetchall()
    ]
    rows.reverse()  # ревью Ф61-1 (2): свежие вопросы не должны выпадать из выдачи за старыми
    replied = _answered_ids(conn, [r["id"] for r in rows])
    refs = {r["ref"] for r in rows if r["ref"]}
    names = titles(refs) if (titles is not None and refs) else {}
    marks = errands(refs) if (errands is not None and refs) else {}
    return [
        {
            "id": r["id"],
            "ts": r["ts"],
            "text": r["text"],
            "ref": r["ref"],
            "ref_title": names.get(r["ref"]) if r["ref"] else None,
            "state": ask_state(r, replied=r["id"] in replied, now_ms=now_ms),
            **_hands(marks, r["ref"]),
        }
        for r in rows
    ]


def tells(conn, *, since_ms: int, now_ms: int, titles=None, errands=None) -> list[dict]:
    """Ф94-1 (A.3): сообщения агента владельцу (`tell`) не старше `since_ms` (не раньше суток назад), последние
    `ASKS_MAX`, по возрастанию id: `[{id, ts, text, ref, ref_title}]`. `titles` — как у `asks`. Ф96-1 (B.1,
    П2): с `errands` — ещё `errand_id`, `hands`, `hands_ts` (как у `asks`) и `handoff` — это «Передаю тебе»
    агента по открытому поручению (`errand_handoff`)."""
    since = clamp_since(since_ms, now_ms)
    rows = conn.execute(
        "SELECT id, ts, text, ref FROM main.owner_talk WHERE tell = 1 AND author = 'agent' AND ts >= ? "
        "ORDER BY id DESC LIMIT ?",
        (since, ASKS_MAX),
    ).fetchall()
    rows.reverse()  # свежие не выпадают из выдачи за старыми
    refs = {r[3] for r in rows if r[3]}
    names = titles(refs) if (titles is not None and refs) else {}
    if errands is None:
        return [
            {
                "id": r[0],
                "ts": r[1],
                "text": r[2],
                "ref": r[3],
                "ref_title": names.get(r[3]) if r[3] else None,
            }
            for r in rows
        ]
    marks = errands(refs) if refs else {}
    out = []
    for r in rows:
        mark = marks.get(r[3]) if r[3] else None
        out.append(
            {
                "id": r[0],
                "ts": r[1],
                "text": r[2],
                "ref": r[3],
                "ref_title": names.get(r[3]) if r[3] else None,
                **_hands(marks, r[3]),
                "handoff": mark is not None and r[0] in mark["handoff_ids"],
            }
        )
    return out


def open_asks(conn, *, now_ms: int) -> dict[str, dict]:
    """Ф65-1 (Ж5): открытые вопросы агента, привязанные к карточке (`ref`), -> `{ref: {id, text, ts,
    until_ts}}` — по карточке самый свежий. Открыт — как у `ask_state`: агент не закрыл, срок не вышел, ответа
    владельца с `reply_to` нет. Ответ из карточки — тот же `POST /owner_channel/talk` с `reply_to` (кука)."""
    rows = [
        _row(r)
        for r in conn.execute(
            f"SELECT {_COLUMNS} FROM main.owner_talk WHERE ask = 1 AND author = 'agent' "  # noqa: S608
            "AND ref IS NOT NULL AND closed_ts IS NULL AND (ask_until_ts IS NULL OR ask_until_ts > ?) "
            "ORDER BY id DESC LIMIT ?",
            (now_ms, ASKS_MAX),
        ).fetchall()
    ]
    replied = _answered_ids(conn, [r["id"] for r in rows])
    out: dict[str, dict] = {}
    for r in rows:
        if r["id"] in replied or r["ref"] in out:
            continue
        out[r["ref"]] = {"id": r["id"], "text": r["text"], "ts": r["ts"], "until_ts": r["ask_until_ts"]}
    return out


def close_ask(conn, talk_id: int, outcome, *, now_ms: int) -> dict:
    """П2: агент закрывает свой вопрос (`answered` — ответ получен, `gave_up` — перестал ждать). Нет реплики —
    404; не вопрос агента — 409; повтор — та же строка без изменений."""
    if outcome not in OUTCOMES:
        raise OwnerError(422, "outcome: answered или gave_up")
    found = _get(conn, talk_id)
    if found is None:
        raise OwnerError(404, "not found")
    if found["author"] != "agent" or not found["ask"]:
        raise OwnerError(409, "это не вопрос агента")
    if found["closed_ts"] is not None:
        return found
    conn.execute(
        "UPDATE main.owner_talk SET closed_ts = ?, closed_outcome = ? WHERE id = ?",
        (now_ms, outcome, talk_id),
    )
    next_seq(conn)
    return _get(conn, talk_id)


def close_asks_of(conn, refs: list[str], *, now_ms: int) -> int:
    """24.10: поручение агента закрылось (вернул владельцу, ответил, остановлен) — его незакрытые вопросы по этим
    карточкам закрываются `gave_up`. Иначе вопрос висит открытым до срока (30 мин), и бот не знает, к какому
    из двух открытых отнести ответ владельца без reply («Ответь на сообщение с вопросом»). Отвеченный вопрос
    остаётся `answered` (`ask_state` смотрит ответ раньше закрытия). -> число закрытых."""
    refs = [r for r in dict.fromkeys(refs) if isinstance(r, str) and r]
    if not refs:
        return 0
    marks = ",".join("?" * len(refs))
    cur = conn.execute(
        "UPDATE main.owner_talk SET closed_ts = ?, closed_outcome = 'gave_up' "  # noqa: S608
        f"WHERE ask = 1 AND author = 'agent' AND closed_ts IS NULL AND ref IN ({marks})",
        (now_ms, *refs),
    )
    if cur.rowcount:
        next_seq(conn)
    return cur.rowcount


def bot_reply(conn, *, reply_to, text, tg_message_id, now_ms: int, audit: dict) -> dict:
    """Ответ владельца из бота Telegram на вопрос агента -> строка (`author = 'owner_channel'`, `via = 'bot'`, `ref` —
    вопроса). П3: повтор с тем же `tg_message_id` — та же строка без второй реплики. Вопрос не открыт —
    `AskNotOpen` (409 со `state`); нет реплики — 404; не вопрос агента — 409. Вопрос отмечается прочитанным
    (иначе красное число на «Написать…» не гаснет)."""
    if isinstance(tg_message_id, bool) or not isinstance(tg_message_id, int) or tg_message_id < 1:
        raise OwnerError(422, "tg_message_id: целое > 0")
    if isinstance(reply_to, bool) or not isinstance(reply_to, int):
        raise OwnerError(422, "reply_to: номер вопроса")
    # Ревью Ф61-1 (1): повтор — тот же номер сообщения Telegram НА ТОТ ЖЕ вопрос. Номера сообщений начинаются
    # заново в новом чате бота (повторный `hub auth human-bot`), так что один номер без вопроса — не повтор.
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM main.owner_talk WHERE via = 'bot' AND tg_msg_id = ? AND reply_to = ?",  # noqa: S608
        (tg_message_id, reply_to),
    ).fetchone()
    if row is not None:
        return _with_files(conn, [_row(row)])[0]
    question = _get(conn, reply_to)
    if question is None:
        raise OwnerError(404, "not found")
    if question["author"] != "agent" or not question["ask"]:
        raise OwnerError(409, "это не вопрос агента")
    state = ask_state(question, replied=bool(_answered_ids(conn, [reply_to])), now_ms=now_ms)
    if state != "open":
        raise AskNotOpen(state)
    text, _ref, _ = _check(conn, text, None, None)
    cur = conn.execute(
        "INSERT INTO main.owner_talk (ts, author, text, ref, reply_to, via, tg_msg_id, session_id) "
        "VALUES (?, 'owner_channel', ?, ?, ?, 'bot', ?, ?)",
        (now_ms, text, question["ref"], reply_to, tg_message_id, question["session_id"]),
    )
    talk_id = cur.lastrowid
    conn.execute(
        "UPDATE main.owner_talk SET read_ts = ? WHERE id = ? AND read_ts IS NULL", (now_ms, reply_to)
    )
    next_seq(conn)
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_talk",
        result="created",
        detail={"id": talk_id, "author": "owner_channel", "via": "bot", "reply_to": reply_to, "len": len(text)},
        **audit,
    )
    return _get(conn, talk_id)


def bot_note_of(conn, reply_to: int, tg_message_id: int) -> dict | None:
    """Ф96-1 (П2): реплика владельца из бота на реплику `reply_to` с этим номером сообщения Telegram (повтор)."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM main.owner_talk WHERE via = 'bot' AND tg_msg_id = ? AND reply_to = ?",  # noqa: S608
        (tg_message_id, reply_to),
    ).fetchone()
    return _with_files(conn, [_row(row)])[0] if row is not None else None


def hands_reply(conn, *, reply_to: int, text: str, tg_message_id, now_ms: int, audit: dict) -> dict:
    """Ф96-1 (B.3, П2): ответ владельца из бота свайпом на реплику агента по поручению («Передаю тебе: …»), с
    которым руки вернулись агенту, -> реплика владельца в ленте «Мой агент» (`via='bot'`, `reply_to`, ветка и
    `ref` — реплики агента). Текст уже ушёл агенту запиской рук (`hands_note` поручения), поэтому реплика
    сразу «обработана» (`handled_ts`): разговор агента не берёт её второй раз. Реплика агента — прочитана.
    Проверки (реплика агента этого поручения, непустой текст) — у вызывающего (`errand_hands`)."""
    question = _get(conn, reply_to)
    if question is None:
        raise OwnerError(404, "not found")
    text, _ref, _ = _check(conn, text, None, None)
    cur = conn.execute(
        "INSERT INTO main.owner_talk "
        "(ts, author, text, ref, reply_to, via, tg_msg_id, session_id, handled_ts) "
        "VALUES (?, 'owner_channel', ?, ?, ?, 'bot', ?, ?, ?)",
        (now_ms, text, question["ref"], reply_to, tg_message_id, question["session_id"], now_ms),
    )
    talk_id = cur.lastrowid
    conn.execute(
        "UPDATE main.owner_talk SET read_ts = ? WHERE id = ? AND read_ts IS NULL", (now_ms, reply_to)
    )
    next_seq(conn)
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_talk",
        result="created",
        detail={"id": talk_id, "author": "owner_channel", "via": "bot", "reply_to": reply_to, "len": len(text)},
        **audit,
    )
    return _get(conn, talk_id)


# --- ветки и пересказ (Ф66-2, docs/architecture.md.3) ----------------------------


def _key(branch) -> int:
    """Ключ строки пересказа: «Общее» — 0."""
    return 0 if branch is None else int(branch)


def _label(rec: held_mod.HubSession) -> str:
    if rec.role:
        return f"{rec.role} {rec.role_no}" if isinstance(rec.role_no, int) and rec.role_no > 1 else rec.role
    return held_mod.shown_name(rec)


def branches(conn, *, project_ids=None) -> list[dict]:
    """Ветки, где есть реплики, плюс «Общее» всегда: `[{session_id, title, project_id, role, last_id,
    last_ts, unread}]`; `title` — «<проект> · <роль>» (без роли — название сессии). С непрочитанным (реплики
    агента без `read_ts`) — сверху, дальше по `last_ts` (новые выше). `project_ids` — скоуп токена."""
    rows = conn.execute(
        "SELECT session_id, MAX(id), MAX(ts), "
        "SUM(CASE WHEN author = 'agent' AND read_ts IS NULL THEN 1 ELSE 0 END) "
        "FROM main.owner_talk GROUP BY session_id"
    ).fetchall()
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM main.project")}
    general = {
        "session_id": None,
        "title": GENERAL_TITLE,
        "project_id": None,
        "role": None,
        "last_id": None,
        "last_ts": None,
        "unread": 0,
    }
    items: list[dict] = [general]
    for session_id, last_id, last_ts, unread in rows:
        if session_id is None:
            general.update(last_id=last_id, last_ts=last_ts, unread=unread or 0)
            continue
        rec = held_mod.get(conn, session_id)
        if rec is None or rec.closed_ts is not None:  # владелец 16.10: закрытые роли в списке не нужны
            continue
        if project_ids is not None and rec.project_id not in project_ids:
            continue
        project = names.get(rec.project_id) or f"проект {rec.project_id}"
        items.append(
            {
                "session_id": rec.id,
                "title": f"{project} · {_label(rec)}",
                "project_id": rec.project_id,
                "role": rec.role,
                "last_id": last_id,
                "last_ts": last_ts,
                "unread": unread or 0,
            }
        )
    items.sort(key=lambda b: (0 if b["unread"] else 1, -(b["last_ts"] or 0)))
    return items


def parse_branch(raw: str | None):
    """Ветка из адреса или запроса: `general` -> `None` («Общее»), цифры -> `int`; нет параметра -> `ALL`.
    Иное — 422."""
    if raw is None:
        return ALL
    if raw == "general":
        return None
    if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 2**53:
        return int(raw)
    raise OwnerError(422, "session_id: номер сессии хаба или general")


def check_branch(conn, branch, *, project_ids=None) -> None:
    """Ветка из адреса существует: «Общее» — всегда, сессия хаба — есть и в скоупе токена (иначе 404)."""
    if branch is None:
        return
    rec = held_mod.get(conn, int(branch))
    if rec is None or (project_ids is not None and rec.project_id not in project_ids):
        raise OwnerError(404, "not found")


def summary(conn, branch) -> dict:
    """Пересказ ветки: `{session_id, summary, upto_id, updated_ts}` (нет строки — пустой, 0, `None`)."""
    row = conn.execute(
        "SELECT summary, summary_upto_id, updated_ts FROM main.owner_talk_branch WHERE session_id = ?",
        (_key(branch),),
    ).fetchone()
    if row is None:
        return {"session_id": branch, "summary": "", "upto_id": 0, "updated_ts": None}
    return {"session_id": branch, "summary": row[0], "upto_id": row[1], "updated_ts": row[2]}


def save_summary(conn, branch, *, text, upto_id, now_ms: int) -> dict:
    """Агент сохраняет пересказ ветки. `text` до `SUMMARY_MAX`; `upto_id` — не больше последней реплики ветки
    и не меньше прежнего (иначе 422). Пересказ в интерфейсе не виден — `seq` не двигается."""
    if not isinstance(text, str) or len(text) > SUMMARY_MAX:
        raise OwnerError(422, f"summary: не длиннее {SUMMARY_MAX} символов")
    if isinstance(upto_id, bool) or not isinstance(upto_id, int) or upto_id < 0:
        raise OwnerError(422, "upto_id: целое ≥ 0")
    where, params = _branch_where(branch)
    last = conn.execute(
        f"SELECT COALESCE(MAX(id), 0) FROM main.owner_talk WHERE {where[0]}",  # noqa: S608
        params,
    ).fetchone()[0]
    if upto_id > last:
        raise OwnerError(422, "upto_id: дальше последней реплики ветки")
    if upto_id < summary(conn, branch)["upto_id"]:
        raise OwnerError(422, "upto_id: меньше прежнего — пересказ назад не двигается")
    conn.execute(
        "INSERT INTO main.owner_talk_branch (session_id, summary, summary_upto_id, updated_ts) "
        "VALUES (?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET summary = excluded.summary, "
        "summary_upto_id = excluded.summary_upto_id, updated_ts = excluded.updated_ts",
        (_key(branch), text, upto_id, now_ms),
    )
    return summary(conn, branch)


def backfill(conn, *, now_ms: int) -> int:
    """Ф66-2 (A.1): старым репликам без ветки — ветка по `ref` (карточка ещё известна — её сессия хаба) или
    по реплике `reply_to`; не вышло — остаётся «Общее». -> сколько строк получили ветку."""
    done: dict[int, int | None] = {}
    moved = 0
    rows = conn.execute("SELECT id, ref, reply_to, session_id FROM main.owner_talk ORDER BY id").fetchall()
    for talk_id, ref, reply_to, session_id in rows:
        if session_id is None:
            if ref:
                session_id = session_mod.session_of_ref(conn, ref, now_ms=now_ms)
            if session_id is None and reply_to is not None:
                session_id = done.get(reply_to)
            if session_id is not None:
                conn.execute("UPDATE main.owner_talk SET session_id = ? WHERE id = ?", (session_id, talk_id))
                moved += 1
        done[talk_id] = session_id
    return moved
