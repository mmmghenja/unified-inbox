"""Ф38-3 C (docs/architecture.md): подсказка о других линиях роли в ответе `POST /subs` —
`other_lines`. Новая сессия зовёт подписку с ролью без `number`, прежней подписки этой сессии нет — номер
берётся наименьший свободный, а с ним режим, ключи и фильтр аккаунтов этой линии. Поведение то же; ядро
называет другие линии роли с ДРУГИМИ настройками (живые и свободные не дольше 7 суток).

Обвязка — `test_f26_multi_subs.py`; в мире два аккаунта, чтобы линии различались фильтром по аккаунтам."""

import pytest

from hub.core import subs

from .conftest import insert_account, insert_channel, insert_project
from .test_f26_multi_subs import A, B, C, _row, _sub, _token

ROLE = "маркетолог"
D = (7171, 1_111_000, "sess-dddddddd-4444")
E = (8181, 1_222_000, "sess-eeeeeeee-5555")
A2 = (A[0], A[1], "sess-aaaaaaaa-after-clear")
DAY_MS = 24 * 60 * 60 * 1000
KEYS = {"keywords": ["штраф"], "exclude": ["вакансия"]}


@pytest.fixture
def procs(monkeypatch):
    table = {who[0]: who[1] for who in (A, B, C, D, E)}
    monkeypatch.setattr(subs, "read_proc_start", lambda pid: table.get(pid))
    return table


@pytest.fixture
def world(db, tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    for name in ("free", "free2"):
        (tmp_path / name).mkdir()
    pid = insert_project(db, name="Umbrella", workdir=str(workdir))
    db.write_sync(lambda conn: subs.set_project_subs(conn, pid, True, now_ms=0))
    acc_a = insert_account(db, source="slack")
    acc_b = insert_account(db, source="slack")
    insert_channel(db, acc_a, project_id=pid, ext_id="GA", kind="group")
    insert_channel(db, acc_b, project_id=pid, ext_id="GB", kind="group")
    return {
        "pid": pid,
        "workdir": str(workdir),
        "free": str(tmp_path / "free"),
        "free2": str(tmp_path / "free2"),
        "A": acc_a,
        "B": acc_b,
        "tok": _token(db),
    }


async def _two_lines(client, w, procs) -> tuple[int, int]:
    """Две линии роли: №1 «только аккаунт A», №2 «все, кроме A»; обе сессии умерли (линии свободны, строки
    ещё не завершены — их завершит следующая подписка)."""
    one = (await _sub(client, w, A, role=ROLE, accounts=[w["A"]]))["sub"]
    two = (await _sub(client, w, B, role=ROLE, accounts_except=[w["A"]]))["sub"]
    assert (one["role_no"], two["role_no"]) == (1, 2)
    del procs[A[0]], procs[B[0]]
    return one["id"], two["id"]


def _brief(lines: list[dict]) -> list[tuple]:
    return [(row["role_no"], row["live"], row["accounts"]) for row in lines]


# --- (а) две свободные линии -------------------------------------------------------------------------------


async def test_a_fresh_session_takes_first_line_and_is_told_about_the_other(client, db, world, procs):
    _one, two = await _two_lines(client, world, procs)
    body = await _sub(client, world, C, role=ROLE)
    assert body["created"] is True and body["sub"]["role_no"] == 1
    assert body["sub"]["accounts"] == {"only": [world["A"]]}  # поведение прежнее: настройки линии №1
    assert _brief(body["other_lines"]) == [(2, False, {"except": [world["A"]]})]
    line = body["other_lines"][0]
    assert line["id"] == two and line["ended_ts"] == _row(db, two).ended_ts is not None
    # форма строки — как у соседа плюс `live` и `ended_ts`
    assert set(line) == {
        "id",
        "role",
        "role_no",
        "session_name",
        "mode",
        "msg_filter",
        "keys",
        "accounts",
        "live",
        "ended_ts",
    }


async def test_a_explicit_number_gives_no_hint(client, db, world, procs):
    await _two_lines(client, world, procs)
    body = await _sub(client, world, C, role=ROLE, number=1)
    assert body["created"] is True and body["sub"]["role_no"] == 1
    assert body["other_lines"] == []


async def test_a_only_counts_of_keys_never_the_keys(client, db, world, procs):
    await _sub(client, world, A, role=ROLE, mode="personal", **KEYS)
    body = await _sub(client, world, B, role=ROLE)
    (line,) = body["other_lines"]
    assert line["keys"] == {"keys": 1, "exclude": 1}
    assert "штраф" not in str(body["other_lines"]) and "вакансия" not in str(body["other_lines"])


# --- (б) живая линия ---------------------------------------------------------------------------------------


async def test_b_live_line_with_other_settings(client, db, world, procs):
    one = (await _sub(client, world, A, role=ROLE, accounts=[world["A"]]))["sub"]["id"]
    body = await _sub(client, world, B, role=ROLE)
    assert body["sub"]["role_no"] == 2
    assert _brief(body["other_lines"]) == [(1, True, {"only": [world["A"]]})]
    assert body["other_lines"][0]["id"] == one and body["other_lines"][0]["ended_ts"] is None


async def test_b_same_settings_give_no_hint(client, db, world, procs):
    await _sub(client, world, A, role=ROLE)
    body = await _sub(client, world, B, role=ROLE)
    assert body["sub"]["role_no"] == 2 and body["other_lines"] == []


async def test_b_same_keys_other_order_is_same_line_settings(client, db, world, procs):
    """Сравниваются разобранные значения, не строки JSON: те же ключи в другом порядке и тот же фильтр
    аккаунтов, записанный иначе, — одинаковые настройки."""
    both = [world["A"], world["B"]]
    await _sub(client, world, A, role=ROLE, mode="personal", keywords=["штраф", "бан"], accounts=both)
    body = await _sub(
        client, world, B, role=ROLE, mode="personal", keywords=["бан", "штраф"], accounts=both[::-1]
    )
    assert body["sub"]["role_no"] == 2 and body["other_lines"] == []
    body = await _sub(client, world, C, role=ROLE, mode="personal", keywords=["бан"])
    assert [row["role_no"] for row in body["other_lines"]] == [1, 2]


async def test_b_own_direct_and_other_line_not_direct(client, db, world, procs):
    await _sub(client, world, A, role=ROLE)
    body = await _sub(client, world, B, role=ROLE, mode="direct")
    assert [(row["role_no"], row["mode"]) for row in body["other_lines"]] == [(1, "cards")]
    body = await _sub(client, world, C, role=ROLE)
    assert [(row["role_no"], row["mode"]) for row in body["other_lines"]] == [(2, "direct")]


# --- (в) своя прежняя подписка есть ------------------------------------------------------------------------


async def test_c_clear_and_repeat_call_give_no_hint(client, db, world, procs):
    await _sub(client, world, A, role=ROLE, accounts=[world["A"]])
    await _sub(client, world, B, role=ROLE, accounts_except=[world["A"]])
    body = await _sub(client, world, A2, role=ROLE)  # /clear: тот же процесс — линия своя
    assert body["created"] is True and body["sub"]["role_no"] == 1
    assert body["other_lines"] == []
    body = await _sub(client, world, A2, role=ROLE)  # повторный вызов живой подписки
    assert body["created"] is False and body["other_lines"] == []
    body = await _sub(client, world, A2)  # и без аргументов
    assert body["created"] is False and body["other_lines"] == []


# --- (г) срок, без роли, вне проектов ----------------------------------------------------------------------


@pytest.mark.parametrize(("days", "shown"), [(6, [2]), (8, [])])
async def test_d_free_line_is_remembered_for_7_days(client, db, world, procs, clock, days, shown):
    await _two_lines(client, world, procs)
    await _sub(client, world, C, role=ROLE)  # завершает обе мёртвые подписки сейчас
    clock.advance_ms(days * DAY_MS)
    del procs[C[0]]
    body = await _sub(client, world, D, role=ROLE)
    assert body["sub"]["role_no"] == 1
    assert [row["role_no"] for row in body["other_lines"]] == shown


async def test_d_no_role_no_hint(client, db, world, procs):
    await _sub(client, world, A, accounts=[world["A"]])
    body = await _sub(client, world, B, accounts_except=[world["A"]])
    assert body["sub"]["role"] is None and body["other_lines"] == []


async def test_d_free_agent_no_hint(client, db, world, procs):
    await _sub(client, {**world, "workdir": world["free"]}, A, mode="direct", role=ROLE)
    body = await _sub(client, {**world, "workdir": world["free2"]}, B, mode="direct", role=ROLE)
    assert body["sub"]["project_id"] == subs.NO_PROJECT_ID and body["sub"]["role_no"] == 2
    assert body["other_lines"] == []


async def test_not_more_than_5_lines_by_number(client, db, world, procs, monkeypatch):
    monkeypatch.setattr(subs, "MAX_PROJECT_SUBS", 10)
    extra = [(9000 + i, 2_000_000 + i, f"sess-xxxxxxxx-{i}") for i in range(4)]
    for who in extra:
        procs[who[0]] = who[1]
    for who in (A, B, C, D, *extra):
        await _sub(client, world, who, role=ROLE, mode="messages")
    body = await _sub(client, world, E, role=ROLE)
    assert body["sub"]["role_no"] == 9
    assert [row["role_no"] for row in body["other_lines"]] == [1, 2, 3, 4, 5]
