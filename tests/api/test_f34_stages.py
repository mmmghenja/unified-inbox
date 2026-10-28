"""Ф34-1: стадии бесед — ядро (`docs/architecture.md` 1–4 и поправки П1–П6): запись
`PATCH /channels/{id}`, bearer со скоупом `organize`, список стадий проекта, фильтр списка бесед, наследование
стадии при слиянии заготовки и `channel_migrate`, миграция `0046_shared.sql` на обеих схемах.
"""

import json
import unicodedata

import pytest

from hub.core import db as db_module
from hub.core import draft_channels, security
from hub.core.db import Db
from hub.core.ingest import apply_batch

from .conftest import (
    csrf_headers,
    insert_account,
    insert_channel,
    insert_message,
    insert_project,
    login_via_code,
)

# --- помощники --------------------------------------------------------------------------------------------


def _stage_row(db, channel_id, schema="main") -> tuple:
    with db.reader() as conn:
        return conn.execute(
            f"SELECT stage, stage_key, stage_note, stage_ts, seq FROM {schema}.channel WHERE id = ?",  # noqa: S608
            (channel_id,),
        ).fetchone()


def _set_stage(db, channel_id, stage, *, note=None, ts=1, schema="main") -> None:
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE {schema}.channel SET stage = ?, stage_key = ?, stage_note = ?, stage_ts = ? "  # noqa: S608
            "WHERE id = ?",
            (stage, stage.strip().casefold(), note, ts, channel_id),
        )
    )


def _audit(db, action="channel_update") -> list[tuple]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT actor, api_token_id, result, channel_id, detail_json FROM main.audit "
            "WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()
    return [(r[0], r[1], r[2], r[3], json.loads(r[4] or "null")) for r in rows]


def _token(db, *, scopes, project_ids=None, name="bearer"):
    created = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind="interactive", scopes=list(scopes), project_ids=project_ids, expires_ts=None
        )
    )
    return created.token, created.token_id


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _now(clock) -> int:
    return int(clock() * 1000)


@pytest.fixture
async def ui(client, db, clock):
    cookie, _csrf = await login_via_code(client, db, clock)
    headers = csrf_headers(cookie)

    class _Ui:
        async def get(self, path, **kw):
            return await client.get(path, **kw)

        async def patch(self, path, body):
            return await client.patch(path, json=body, headers=headers)

    return _Ui()


@pytest.fixture
def stand(db):
    project = insert_project(db, name="Umbrella")
    account = insert_account(db)
    channel = insert_channel(db, account, project_id=project, ext_id="c1", title="Партнёр", last_msg_ts=100)
    return {"project": project, "account": account, "channel": channel}


# --- B1: поставить, сменить, снять; длина и переводы строк; seq и /sync -----------------------------------


async def test_b1_set_change_clear_stage(ui, db, clock, stand):
    ch = stand["channel"]
    seq0 = _stage_row(db, ch)[4]
    max_seq0 = (await ui.get("/api/v1/sync", params={"since": 0})).json()["max_seq"]

    body = {"stage": "  Ждём ответа ", "stage_note": " догон до 07.10 "}
    r = await ui.patch(f"/api/v1/channels/{ch}", body)
    assert r.status_code == 200, r.text
    set_at = _now(clock)
    stage, key, note, ts, seq1 = _stage_row(db, ch)
    assert (stage, key, note, ts) == ("Ждём ответа", "ждём ответа", "догон до 07.10", set_at)
    assert seq1 > seq0

    # `/sync` отдаёт беседу со стадией; `GET /channels/{id}` и список бесед — те же поля
    sync = (await ui.get("/api/v1/sync", params={"since": max_seq0})).json()
    (changed,) = [c for c in sync["channels"] if c["id"] == ch]
    expected = {"stage": "Ждём ответа", "stage_note": "догон до 07.10", "stage_ts": set_at}
    assert {k: changed[k] for k in expected} == expected
    one = (await ui.get(f"/api/v1/channels/{ch}")).json()
    assert {k: one[k] for k in expected} == expected
    (listed,) = (await ui.get(f"/api/v1/projects/{stand['project']}/channels")).json()
    assert {k: listed[k] for k in expected} == expected

    # то же самое в другом написании без заметки — ключ прежний, заметка остаётся
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage": "ждём ответа"})
    assert r.status_code == 200, r.text
    assert _stage_row(db, ch)[2] == "догон до 07.10"

    # сменить стадию: время двигается, заметка прежней стадии стирается
    clock.advance_ms(5000)
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage": "Отказ"})
    assert r.status_code == 200, r.text
    stage, key, note, ts, seq2 = _stage_row(db, ch)
    assert (stage, key, note, ts) == ("Отказ", "отказ", None, _now(clock))
    assert seq2 > seq1

    # снять: пустая строка — стадии нет, заметка вычищена (П6)
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage": ""})
    assert r.status_code == 200, r.text
    stage, key, note, ts, seq3 = _stage_row(db, ch)
    assert (stage, key, note, ts) == (None, None, None, None)
    assert seq3 > seq2
    one = (await ui.get(f"/api/v1/channels/{ch}")).json()
    assert (one["stage"], one["stage_note"], one["stage_ts"]) == (None, None, None)

    # снять через `null` — то же самое
    await ui.patch(f"/api/v1/channels/{ch}", {"stage": "В работе", "stage_note": "x"})
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage": None})
    assert r.status_code == 200, r.text
    assert _stage_row(db, ch)[:4] == (None, None, None, None)


@pytest.mark.parametrize(
    "body",
    [
        {"stage": "я" * 41},
        {"stage": "в\nработе"},
        {"stage": "в\rработе"},
        {"stage": "в работе"},
        {"stage": "ок", "stage_note": "з" * 201},
        {"stage": "ок", "stage_note": "раз\nдва"},
    ],
)
async def test_b1_length_and_line_breaks_are_422(ui, db, stand, body):
    ch = stand["channel"]
    before = _stage_row(db, ch)
    r = await ui.patch(f"/api/v1/channels/{ch}", body)
    assert r.status_code == 422, r.text
    assert _stage_row(db, ch) == before
    assert _audit(db) == []


async def test_b1_limits_are_inclusive_after_strip(ui, db, stand):
    ch = stand["channel"]
    body = {"stage": " " + "я" * 40 + " ", "stage_note": "з" * 200}
    r = await ui.patch(f"/api/v1/channels/{ch}", body)
    assert r.status_code == 200, r.text
    stage, _key, note, _ts, _seq = _stage_row(db, ch)
    assert (len(stage), len(note)) == (40, 200)


# --- П6: заметка без стадии, время стадии, seq ------------------------------------------------------------


@pytest.mark.parametrize("body", [{"stage_note": "заметка"}, {"stage": "", "stage_note": "заметка"}])
async def test_p6_note_without_stage_is_422(ui, db, stand, body):
    ch = stand["channel"]
    before = _stage_row(db, ch)
    r = await ui.patch(f"/api/v1/channels/{ch}", body)
    assert r.status_code == 422, r.text
    assert _stage_row(db, ch) == before


async def test_p6_note_change_keeps_stage_ts_and_moves_seq(ui, db, clock, stand):
    ch = stand["channel"]
    await ui.patch(f"/api/v1/channels/{ch}", {"stage": "В работе"})
    _stage, _key, _note, ts0, seq0 = _stage_row(db, ch)

    clock.advance_ms(7000)
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage_note": "жду договор"})
    assert r.status_code == 200, r.text
    stage, _key, note, ts1, seq1 = _stage_row(db, ch)
    assert (stage, note, ts1) == ("В работе", "жду договор", ts0)  # заметка время стадии не двигает
    assert seq1 > seq0

    # та же стадия с новой заметкой — время стадии на месте
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage": "В работе", "stage_note": "подписали"})
    assert r.status_code == 200, r.text
    _stage, _key, note, ts2, seq2 = _stage_row(db, ch)
    assert (note, ts2) == ("подписали", ts0) and seq2 > seq1

    # убрать заметку, стадия остаётся
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage_note": ""})
    assert r.status_code == 200, r.text
    stage, _key, note, ts3, seq3 = _stage_row(db, ch)
    assert (stage, note, ts3) == ("В работе", None, ts0) and seq3 > seq2

    # ничего не поменялось — ничего не пишется
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage": " В работе "})
    assert r.status_code == 200, r.text
    assert _stage_row(db, ch) == ("В работе", "в работе", None, ts0, seq3)


# --- П1: заметка не попадает в аудит ----------------------------------------------------------------------


async def test_p1_audit_has_stage_but_not_note(ui, client, db, stand):
    ch = stand["channel"]
    body = {"stage": "Ждём ответа", "stage_note": "секретная заметка"}
    r = await ui.patch(f"/api/v1/channels/{ch}", body)
    assert r.status_code == 200, r.text
    r = await ui.patch(f"/api/v1/channels/{ch}", {"stage_note": "другая секретная"})
    assert r.status_code == 200, r.text
    rows = _audit(db)
    assert [row[4] for row in rows] == [{"stage": "Ждём ответа"}, None]
    with db.reader() as conn:
        raw = conn.execute("SELECT COALESCE(GROUP_CONCAT(detail_json), '') FROM main.audit").fetchone()[0]
    assert "секретная" not in raw and "stage_note" not in raw


async def test_p1_denied_widens_scope_audit_has_no_note(client, db, clock, stand):
    """Ветка отказа `widens_scope`: перенос со стадией и заметкой в одном запросе — заметки в аудите нет."""
    other = insert_project(db, name="P2")
    _token(db, scopes=("read",), project_ids=[other], name="mcp:p2")
    token, _tid = _token(db, scopes=("read", "organize"), name="mcp:all")
    ch = stand["channel"]
    r = await client.patch(
        f"/api/v1/channels/{ch}",
        json={"project_id": other, "stage": "Отказ", "stage_note": "секретная заметка"},
        headers=_bearer(token),
    )
    assert r.status_code == 409, r.text
    assert _stage_row(db, ch)[:3] == (None, None, None)
    (row,) = _audit(db)
    assert row[2] == "denied_widens_scope"
    assert row[4] == {"project_id": other, "stage": "Отказ", "widens_scope_tokens": ["mcp:p2"]}


# --- B2: bearer -------------------------------------------------------------------------------------------


async def test_b2_bearer_with_organize_sets_stage(client, db, clock, stand):
    ch = stand["channel"]
    token, tid = _token(db, scopes=("read", "organize"))
    r = await client.patch(
        f"/api/v1/channels/{ch}", json={"stage": "В работе", "stage_note": "догон"}, headers=_bearer(token)
    )
    assert r.status_code == 200, r.text
    assert _stage_row(db, ch)[:3] == ("В работе", "в работе", "догон")
    (row,) = _audit(db)
    assert row == ("interactive", tid, "ok", ch, {"stage": "В работе"})
    # проект и раскладка стадией не тронуты
    with db.reader() as conn:
        assert conn.execute("SELECT project_id FROM main.channel WHERE id = ?", (ch,)).fetchone() == (
            stand["project"],
        )


async def test_b2_bearer_without_organize_is_403(client, db, stand):
    ch = stand["channel"]
    token, tid = _token(db, scopes=("read", "draft"), project_ids=[stand["project"]])
    r = await client.patch(f"/api/v1/channels/{ch}", json={"stage": "В работе"}, headers=_bearer(token))
    assert r.status_code == 403
    assert _stage_row(db, ch)[:3] == (None, None, None)
    denied = _audit(db, "ui_only_denied")
    assert len(denied) == 1 and denied[0][1] == tid


async def test_b2_bearer_unknown_channel_is_404(client, db, stand):
    token, _tid = _token(db, scopes=("read", "organize"))
    r = await client.patch("/api/v1/channels/999999", json={"stage": "В работе"}, headers=_bearer(token))
    assert r.status_code == 404


@pytest.mark.parametrize("body", [{"stage": "x", "muted": True}, {"stage": "x", "attention": None}])
async def test_b2_other_fields_still_403_for_bearer(client, db, stand, body):
    ch = stand["channel"]
    token, _tid = _token(db, scopes=("read", "organize"))
    r = await client.patch(f"/api/v1/channels/{ch}", json=body, headers=_bearer(token))
    assert r.status_code == 403
    assert _stage_row(db, ch)[:3] == (None, None, None)
    assert _audit(db) == []


async def test_b2_stage_only_patch_is_not_a_move(client, db, stand):
    """Поправки, вводная: PATCH только со стадией от bearer не упирается в проверку переноса (`widens_scope`)
    даже при проектном токене, которому беседа не видна."""
    other = insert_project(db, name="P2")
    _token(db, scopes=("read",), project_ids=[other], name="mcp:p2")
    token, _tid = _token(db, scopes=("read", "organize"), name="mcp:all")
    unsorted = insert_channel(db, stand["account"], ext_id="c-unsorted")
    for ch in (stand["channel"], unsorted):
        r = await client.patch(f"/api/v1/channels/{ch}", json={"stage": "В работе"}, headers=_bearer(token))
        assert r.status_code == 200, r.text
        assert _stage_row(db, ch)[0] == "В работе"


# --- B3 + П3 + П4: список стадий проекта и фильтр списка бесед --------------------------------------------


async def test_b3_stages_merge_case_and_spaces_cyrillic(ui, db, clock, stand):
    project, account = stand["project"], stand["account"]
    a = stand["channel"]
    b = insert_channel(db, account, project_id=project, ext_id="c2", title="Б", last_msg_ts=200)
    c = insert_channel(db, account, project_id=project, ext_id="c3", title="В", last_msg_ts=300)
    d = insert_channel(db, account, project_id=project, ext_id="c4", title="Г", last_msg_ts=400)
    plain = insert_channel(db, account, project_id=project, ext_id="c5", title="Д", last_msg_ts=500)
    elsewhere = insert_channel(db, account, project_id=insert_project(db, name="P2"), ext_id="c6")

    await ui.patch(f"/api/v1/channels/{a}", {"stage": "В работе"})
    clock.advance_ms(1000)
    await ui.patch(f"/api/v1/channels/{b}", {"stage": "в работе "})
    clock.advance_ms(1000)
    await ui.patch(f"/api/v1/channels/{c}", {"stage": "ОТКАЗ"})
    await ui.patch(f"/api/v1/channels/{d}", {"stage": "В РАБОТЕ"})
    await ui.patch(f"/api/v1/channels/{elsewhere}", {"stage": "Чужая"})
    # ключ считает Python: SQLite `lower()` кириллицу не трогает
    assert {_stage_row(db, ch)[1] for ch in (a, b, d)} == {"в работе"}

    r = await ui.get(f"/api/v1/projects/{project}/stages")
    assert r.status_code == 200, r.text
    # показывается как записано последним (самый свежий `stage_ts` ключа); порядок — по ключу
    assert r.json() == [{"stage": "В РАБОТЕ", "count": 3}, {"stage": "ОТКАЗ", "count": 1}]

    # фильтр списка бесед: по стадии (регистр и пробелы не важны), «без стадии», без фильтра
    async def ids(**params):
        rows = (await ui.get(f"/api/v1/projects/{project}/channels", params=params)).json()
        return [row["id"] for row in rows]

    assert await ids(stage=" В Работе ") == [d, b, a]
    assert await ids(stage="отказ") == [c]
    assert await ids(stage="нет такой") == []
    assert await ids(stage="") == [plain]
    assert await ids() == [plain, d, c, b, a]


async def test_b3_p4_archived_hidden_and_forum_are_not_counted(ui, db, clock, stand):
    """П4: счётчик стадии обязан сходиться со списком бесед — тот же предикат (без форумов-контейнеров, без
    удалённых в хабе) и без архивных."""
    project, account = stand["project"], stand["account"]
    live = stand["channel"]
    archived = insert_channel(db, account, project_id=project, ext_id="a1", archived=1)
    hidden = insert_channel(db, account, project_id=project, ext_id="h1")
    hidden_with_new = insert_channel(db, account, project_id=project, ext_id="h2")
    forum = insert_channel(db, account, project_id=project, ext_id="f1", kind="group")
    topic = insert_channel(
        db, account, project_id=project, ext_id="f1:t1", kind="topic", parent_channel_id=forum
    )
    work_account = insert_account(db, source="slack", db_name="work")
    work = insert_channel(db, work_account, project_id=project, ext_id="w1", db_name="work")
    for ch in (live, archived, hidden, hidden_with_new, forum, topic):
        _set_stage(db, ch, "В работе")
    _set_stage(db, work, "в работе", schema="work")
    insert_message(db, hidden_with_new, ext_id="1", ts=50)
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE main.channel SET hidden_before_ts = 10 WHERE id IN (?, ?)", (hidden, hidden_with_new)
        )
    )

    stages = (await ui.get(f"/api/v1/projects/{project}/stages")).json()
    assert [(s["stage"].casefold(), s["count"]) for s in stages] == [("в работе", 4)]

    rows = (await ui.get(f"/api/v1/projects/{project}/channels", params={"stage": "в работе"})).json()
    assert {r["id"] for r in rows} == {live, archived, hidden_with_new, topic, work}
    assert {r["id"] for r in rows if not r["archived"]} == {live, hidden_with_new, topic, work}
    assert len([r for r in rows if not r["archived"]]) == stages[0]["count"]

    # архив стадию сохраняет: вернули из архива — снова в счёте
    await ui.patch(f"/api/v1/channels/{archived}", {"archived": False})
    assert (await ui.get(f"/api/v1/projects/{project}/stages")).json()[0]["count"] == 5


async def test_b3_stages_scope_and_pseudo_projects(ui, client, db, stand):
    project = stand["project"]
    other = insert_project(db, name="P2")
    unsorted = insert_channel(db, stand["account"], ext_id="u1")
    _set_stage(db, stand["channel"], "В работе")
    _set_stage(db, unsorted, "Разобрать")

    assert (await ui.get("/api/v1/projects/-1/stages")).json() == [{"stage": "Разобрать", "count": 1}]
    assert (await ui.get("/api/v1/projects/-2/stages")).json() == [
        {"stage": "В работе", "count": 1},
        {"stage": "Разобрать", "count": 1},
    ]
    assert (await ui.get(f"/api/v1/projects/{other}/stages")).json() == []
    assert (await ui.get("/api/v1/projects/999/stages")).status_code == 404

    # проектный токен: свой проект — видит, чужой и псевдо — 404; без токена — 401
    token, _tid = _token(db, scopes=("read",), project_ids=[other])
    client.cookies.clear()
    for pid, status in ((other, 200), (project, 404), (-1, 404), (-2, 404)):
        r = await client.get(f"/api/v1/projects/{pid}/stages", headers=_bearer(token))
        assert r.status_code == status, (pid, r.text)
    assert (await client.get(f"/api/v1/projects/{project}/stages")).status_code == 401


# --- B4: перенос в другой проект; слияние заготовки; channel_migrate (П2) ---------------------------------


async def test_b4_move_to_other_project_keeps_stage(ui, db, stand):
    ch = stand["channel"]
    other = insert_project(db, name="P2")
    await ui.patch(f"/api/v1/channels/{ch}", {"stage": "В работе", "stage_note": "заметка"})
    before = _stage_row(db, ch)[:4]

    r = await ui.patch(f"/api/v1/channels/{ch}", {"project_id": other})
    assert r.status_code == 200, r.text
    assert _stage_row(db, ch)[:4] == before
    assert (await ui.get(f"/api/v1/projects/{other}/stages")).json() == [{"stage": "В работе", "count": 1}]
    assert (await ui.get(f"/api/v1/projects/{stand['project']}/stages")).json() == []


@pytest.mark.parametrize("target_has_stage", [False, True])
def test_b4_merge_draft_channel_stage(db, stand, target_has_stage):
    """Решение 4: стадия заготовки переезжает, если у настоящей беседы стадии нет; иначе — её стадия."""
    account, project = stand["account"], stand["project"]
    draft = insert_channel(db, account, project_id=project, ext_id="new:abc", title="@petr")
    target = insert_channel(db, account, ext_id="555", title="Пётр")
    _set_stage(db, draft, "Написал", note="первое касание", ts=111)
    if target_has_stage:
        _set_stage(db, target, "Ответил", note="своя", ts=222)
    seq0 = _stage_row(db, target)[4]

    db.write_sync(
        lambda conn: draft_channels._merge(
            conn, "main", draft, target, outbox_id=None, now_ms=5000, via="report"
        )
    )

    stage, key, note, ts, seq1 = _stage_row(db, target)
    if target_has_stage:
        assert (stage, key, note, ts) == ("Ответил", "ответил", "своя", 222)
    else:
        assert (stage, key, note, ts) == ("Написал", "написал", "первое касание", 111)
    assert seq1 > seq0
    with db.reader() as conn:
        assert conn.execute("SELECT archived FROM main.channel WHERE id = ?", (draft,)).fetchone() == (1,)


def _migrate(db, account_id, old_ext_id, new_ext_id, *, kind, attrs=None):
    channel = {"ext_id": new_ext_id, "kind": kind}
    if attrs is not None:
        channel["attrs"] = attrs
    event = {"type": "channel_migrate", "old_ext_id": old_ext_id, "channel": channel}
    return db.write_sync(
        lambda conn: apply_batch(conn, account_id=account_id, events=[event], cursor=None, now_ms=9000)
    )


@pytest.mark.parametrize("schema", ["main", "work"])
def test_p2_migrate_group_to_supergroup_inherits_stage(db, schema):
    account = insert_account(db, db_name=schema)
    project = insert_project(db, name="P")
    old = insert_channel(db, account, project_id=project, ext_id="-100", kind="group", db_name=schema)
    _set_stage(db, old, "В работе", note="ждём", ts=333, schema=schema)

    assert _migrate(db, account, "-100", "-1001", kind="group").applied == 1

    with db.reader() as conn:
        new_id = conn.execute(
            f"SELECT id FROM {schema}.channel WHERE ext_id = '-1001'"  # noqa: S608
        ).fetchone()[0]
    assert _stage_row(db, new_id, schema)[:4] == ("В работе", "в работе", "ждём", 333)
    # старая беседа — в архиве, стадию сохраняет, но в счёт не входит
    with db.reader() as conn:
        archived = conn.execute(
            f"SELECT archived FROM {schema}.channel WHERE id = ?",  # noqa: S608
            (old,),
        ).fetchone()[0]
    assert archived == 1 and _stage_row(db, old, schema)[0] == "В работе"


def test_p2_migrate_keeps_stage_of_existing_new_channel(db):
    """Новая беседа появилась раньше события и уже со своей стадией (и размечена руками) — стадия её."""
    account = insert_account(db)
    old = insert_channel(db, account, ext_id="-100", kind="group")
    new = insert_channel(db, account, ext_id="-1001", kind="group")
    _set_stage(db, old, "Старая", ts=1)
    _set_stage(db, new, "Своя", note="n", ts=2)

    assert _migrate(db, account, "-100", "-1001", kind="group").applied == 1
    assert _stage_row(db, new)[:4] == ("Своя", "своя", "n", 2)


def test_p2_migrate_existing_manual_channel_without_stage_inherits_and_moves_seq(db):
    """Наследование стадии не зависит от наследования раскладки: новая беседа размечена руками (раскладка
    старой к ней не переезжает), стадии у неё нет — стадия переезжает, `seq` двигается."""
    account = insert_account(db)
    p_old, p_new = insert_project(db, name="old"), insert_project(db, name="new")
    old = insert_channel(db, account, project_id=p_old, ext_id="-100", kind="group")
    new = insert_channel(db, account, project_id=p_new, ext_id="-1001", kind="group")
    db.write_sync(
        lambda conn: conn.execute("UPDATE main.channel SET map_source = 'manual' WHERE id = ?", (new,))
    )
    _set_stage(db, old, "В работе", ts=5)
    seq0 = _stage_row(db, new)[4]

    assert _migrate(db, account, "-100", "-1001", kind="group").applied == 1
    stage, _key, _note, ts, seq1 = _stage_row(db, new)
    assert (stage, ts) == ("В работе", 5) and seq1 > (seq0 or 0)
    with db.reader() as conn:
        assert conn.execute("SELECT project_id FROM main.channel WHERE id = ?", (new,)).fetchone() == (p_new,)


def test_p2_migrate_mail_threads_into_tracker_channel(db):
    """Треды почты -> беседа трекера (Ф10-5): несколько старых веток вливаются в одну беседу задачи — стадия
    первой ветки со стадией переезжает, следующая её не перетирает."""
    account = insert_account(db, source="mail")
    project = insert_project(db, name="Initech")
    t1 = insert_channel(db, account, project_id=project, ext_id="thr1", kind="mail_thread")
    t2 = insert_channel(db, account, project_id=project, ext_id="thr2", kind="mail_thread")
    t3 = insert_channel(db, account, project_id=project, ext_id="thr3", kind="mail_thread")
    _set_stage(db, t2, "В работе", note="из второй ветки", ts=20)
    _set_stage(db, t3, "Отказ", ts=30)
    tracker = "jira:acme.atlassian.net:WEB-39"
    attrs = {"tracker": "jira", "key": "WEB-39", "url": "https://acme.atlassian.net/browse/WEB-39"}

    for old_ext in ("thr1", "thr2", "thr3"):
        assert _migrate(db, account, old_ext, tracker, kind="issue", attrs=attrs).applied == 1

    with db.reader() as conn:
        new_id = conn.execute("SELECT id FROM main.channel WHERE ext_id = ?", (tracker,)).fetchone()[0]
    assert _stage_row(db, new_id)[:4] == ("В работе", "в работе", "из второй ветки", 20)
    assert t1 != new_id


# --- миграция 0046 на обеих схемах ------------------------------------------------------------------------


def test_migration_0046_on_previous_version_both_files(tmp_path, monkeypatch):
    original = db_module._migrations

    def _up_to_45(*, main: bool):
        return {v: files for v, files in original(main=main).items() if v <= 45}

    monkeypatch.setattr(db_module, "_migrations", _up_to_45)
    old = Db(tmp_path)
    old.start()
    ids = {}
    try:
        for schema in ("main", "work"):
            account = insert_account(old, db_name=schema)
            ids[schema] = insert_channel(old, account, ext_id="c1", db_name=schema, title="старая")
        with old.reader() as conn:
            for schema in ("main", "work"):
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(channel)")}
                assert "attention" in cols and not cols & {"stage", "stage_key", "stage_note", "stage_ts"}
    finally:
        old.close()

    monkeypatch.setattr(db_module, "_migrations", original)
    new = Db(tmp_path)
    new.start()
    try:
        with new.reader() as conn:
            for schema in ("main", "work"):
                assert conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] >= 46
                cols = {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(channel)")}
                assert {"stage", "stage_key", "stage_note", "stage_ts"} <= cols
                indexes = {r[1] for r in conn.execute(f"PRAGMA {schema}.index_list(channel)")}
                assert "channel_stage" in indexes
        for schema in ("main", "work"):
            assert _stage_row(new, ids[schema], schema)[:4] == (None, None, None, None)
    finally:
        new.close()


@pytest.mark.parametrize("stage", ["​", "⁠﻿", "\x00", "‮"])
async def test_review_invisible_stage_clears(ui, db, stand, stage):
    """Ревью Ф34-1: стадия из одних невидимых символов — не стадия (пустого чипа не бывает)."""
    ch = stand["channel"]
    await ui.patch(f"/api/v1/channels/{ch}", {"stage": stage})
    assert _stage_row(db, ch)[0] is None


async def test_review_zero_width_and_nfd_are_one_stage(ui, db, stand):
    """Ревью Ф34-1: «Мой клиент» в NFC, в NFD (macOS/iOS) и с символом нулевой ширины — одна стадия."""
    ch = stand["channel"]
    acc = stand["account"]
    ch2 = insert_channel(db, acc, project_id=stand["project"], ext_id="c2", title="Второй", last_msg_ts=50)
    ch3 = insert_channel(db, acc, project_id=stand["project"], ext_id="c3", title="Третий", last_msg_ts=40)
    assert (await ui.patch(f"/api/v1/channels/{ch}", {"stage": "Мой клиент"})).status_code == 200
    assert (await ui.patch(f"/api/v1/channels/{ch2}", {"stage": "Мой клиент​"})).status_code == 200
    nfd = unicodedata.normalize("NFD", "Мой клиент")
    assert (await ui.patch(f"/api/v1/channels/{ch3}", {"stage": nfd})).status_code == 200
    stages = (await ui.get(f"/api/v1/projects/{stand['project']}/stages")).json()
    assert [s["count"] for s in stages] == [3], stages
