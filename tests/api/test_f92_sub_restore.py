"""Ф92-1, раздел B (docs/architecture.md): подписки на беседы не теряются,
когда подписку роли переоформляют.

- B2: у новой линии роли нет своего источника — источник берётся с другой линии той же роли, чья линия сейчас
  никем не держится; беседу, которую держит живая сессия роли, не трогаем (`busy`).
- B3: смена токена той же сессией (`replaced`), перевыпуск токена профиля (`hub mcp setup` ->
  `token_reissued`) и подписка той же сессии, снятая ядром как «умершая», — беседы возвращаются; явный отзыв
  токена владельцем (`hub token revoke`) — насовсем.
- B4: подписка с ролью без источника — `restore_blocked: "no_source"` и строка аудита; есть источник —
  `restore_source`.
- B5: `GET /admin/subs?all=1&days=N` — завершённые за N суток.

Мир и помощники — test_f32_chan_subs_restore.py."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import chan_subs, subs
from hub.core.app_internal import create_internal_app
from hub.core.connector_auth import ADMIN_PREFIX, hash_secret

from . import test_f32_chan_subs_restore as f32
from .test_f32_chan_subs_restore import (
    DAY_MS,
    MIN_MS,
    ROLE,
    A,
    B,
    C,
    _a_with_two,
    _chan_rows,
    _chan_sub,
    _cleanup,
    _live,
    _now,
    _restored,
    _skipped,
    _sub,
    _sub_reason,
)

procs = f32.procs
world = f32.world

ADMIN_SECRET = ADMIN_PREFIX + "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHI"
D = (8181, 222_000, "sess-dddddddd-4444")


@pytest.fixture
async def admin_client(db, clock, app):
    internal = create_internal_app(
        db,
        clock=clock,
        notifier=app.state.notifier,
        admin_key_hash=hash_secret(ADMIN_SECRET),
        sub_waiters=app.state.sub_waiters,
        chan_sub_waiters=app.state.chan_sub_waiters,
    )
    async with AsyncClient(transport=ASGITransport(app=internal), base_url="http://hub-internal") as c:
        yield c


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_SECRET}"}


def _token_id(db, token: str) -> int:
    from hub.core import security

    with db.reader() as conn:
        return conn.execute(
            "SELECT id FROM main.api_token WHERE hash = ?",
            (security._sha256(token),),  # noqa: SLF001
        ).fetchone()[0]


def _audit(db, action: str) -> list[tuple[str | None, dict]]:
    with db.reader() as conn:
        rows = conn.execute(
            "SELECT result, detail_json FROM main.audit WHERE action = ? ORDER BY id", (action,)
        ).fetchall()
    return [(result, json.loads(detail) if detail else {}) for result, detail in rows]


# --- B2: источник с другой линии роли ---------------------------------------------------------------------


async def test_b2_new_line_takes_free_line_source_but_not_busy_channel(client, db, world, procs, clock):
    """Живая «маркетолог» №1 держит G1; закрытая «маркетолог» №2 слушала G1 и G2; новая «маркетолог» №3 (своей
    прежней линии нет) получает G2 и не трогает G1 живой сессии роли."""
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    await _sub(client, world, B, role=ROLE, number=1)
    b_g1 = await _chan_sub(client, world, B, "G1")
    await _sub(client, world, A, role=ROLE, number=2)
    await _chan_sub(client, world, A, "G1")
    await _chan_sub(client, world, A, "G2")
    del procs[A[0]]
    clock.advance_ms(2 * MIN_MS)
    _cleanup(db, clock)
    body = await _sub(client, world, C, role=ROLE, number=3)
    assert body["sub"]["role_no"] == 3
    assert set(_live(db, C)) == {g2}
    assert set(_restored(body)) == {g2}
    assert _skipped(body) == {g1: f"busy:#{b_g1}"}
    assert _live(db, B) == {g1: b_g1}  # беседу живой сессии роли не тронули


async def test_b2_line_held_live_is_not_a_source(client, db, world, procs, clock):
    """Источник с чужой линии — только с линии, которую сейчас никто не держит: последняя завершённая подписка
    роли с живой линии (её беседы уже у живой) источником не становится."""
    g1, g2, g3 = world["ch"]["G1"], world["ch"]["G2"], world["ch"]["G3"]
    await _sub(client, world, A, role=ROLE, number=2)
    await _chan_sub(client, world, A, "G1")
    await _chan_sub(client, world, A, "G2")
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    # Линия №1: прежняя сессия кончилась ПОЗЖЕ линии №2, её беседу G3 вернула себе новая сессия той же линии.
    procs[D[0]] = D[1]
    await _sub(client, world, D, role=ROLE, number=1, restore_channels=False)
    await _chan_sub(client, world, D, "G3")
    procs.pop(D[0])
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    first = await _sub(client, world, B, role=ROLE, number=1)
    assert set(_restored(first)) == {g3}
    body = await _sub(client, world, C, role=ROLE, number=3)
    assert set(_restored(body)) == {g1, g2} and set(_live(db, B)) == {g3}


async def test_b2_own_line_source_still_first(client, db, world, procs, clock):
    """Своя прежняя линия есть — берётся она (как в Ф32-1), источник с другой линии не нужен."""
    await _a_with_two(client, world)  # «маркетолог» №1: G1, G2
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    assert body["sub"]["role_no"] == 1 and len(body["restored_channels"]) == 2


# --- B3: смена токена и перевыпуск профиля ---------------------------------------------------------------


async def test_b3_same_session_new_token_restores(client, db, world, procs, clock):
    """Та же сессия (pid, starttime, session_id) подписалась другим токеном — прежняя подписка `replaced`, её
    беседы под прежним токеном переходят новой (раньше: `restore_blocked: replaced`, беседы висли)."""
    sub_a, c1, c2 = await _a_with_two(client, world)
    other = f32._token(db, name="mcp:desktop")
    body = await _sub(client, world, A, role=ROLE, token=other)
    assert _sub_reason(db, sub_a) == "replaced"
    g1, g2 = world["ch"]["G1"], world["ch"]["G2"]
    assert set(_restored(body)) == {g1, g2} and "restore_blocked" not in body
    rows = {r[0]: r for r in _chan_rows(db)}
    assert (rows[c1][3], rows[c2][3]) == ("replaced", "replaced")
    with db.reader() as conn:
        tokens = {
            row[0]
            for row in conn.execute(
                "SELECT api_token_id FROM main.session_chan_sub WHERE ended_ts IS NULL"
            ).fetchall()
        }
    assert tokens == {_token_id(db, other)}


async def test_b3_same_session_without_role_new_token_restores(client, db, world, procs, clock):
    """И без роли: та же сессия под новым токеном — свои беседы при ней."""
    await _sub(client, world, A, mode="messages")
    await _chan_sub(client, world, A, "G1")
    other = f32._token(db, name="mcp:desktop")
    body = await _sub(client, world, A, token=other)
    assert set(_restored(body)) == {world["ch"]["G1"]}


async def test_b3_profile_reissue_is_restorable(client, admin_client, db, world, procs, clock):
    """`hub mcp setup` перевыпускает токен профиля: прежний отзывается с `replaced_by` — подписки этого токена
    кончаются `token_reissued` сразу, и сессия под новым токеном получает свои беседы."""
    sub_a, c1, c2 = await _a_with_two(client, world)
    old_id = _token_id(db, world["tok"])
    new = f32._token(db, name="mcp:all")
    new_id = _token_id(db, new)
    r = await admin_client.post(
        f"/internal/v1/admin/tokens/{old_id}/revoke", json={"replaced_by": new_id}, headers=_admin()
    )
    assert r.status_code == 200, r.text
    assert _sub_reason(db, sub_a) == "token_reissued"
    rows = {r[0]: r for r in _chan_rows(db)}
    assert (rows[c1][3], rows[c2][3]) == ("token_reissued", "token_reissued")
    _cleanup(db, clock)
    body = await _sub(client, world, A, role=ROLE, token=new)
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}
    assert _audit(db, "token_revoke")[-1][1] == {"replaced_by": new_id}
    # Другой разговор той же роли (сессия сменилась) — тоже: причина «сама».
    sub_b = body["sub"]["id"]
    newer = f32._token(db, name="mcp:all")
    r = await admin_client.post(
        f"/internal/v1/admin/tokens/{new_id}/revoke",
        json={"replaced_by": _token_id(db, newer)},
        headers=_admin(),
    )
    assert r.status_code == 200 and _sub_reason(db, sub_b) == "token_reissued"
    del procs[A[0]]
    got = await _sub(client, world, B, role=ROLE, token=newer)
    assert len(got["restored_channels"]) == 2


async def test_b3_reissue_refusals(client, admin_client, db, world, procs, clock):
    """`replaced_by` — только живой bearer-токен с тем же именем; иначе 422 и ничего не отозвано."""
    sub_a, _c1, _c2 = await _a_with_two(client, world)
    old_id = _token_id(db, world["tok"])
    for target in (old_id, 99_999, _token_id(db, f32._token(db, name="чужой"))):
        r = await admin_client.post(
            f"/internal/v1/admin/tokens/{old_id}/revoke", json={"replaced_by": target}, headers=_admin()
        )
        assert r.status_code == 422, r.text
    assert _sub_reason(db, sub_a) is None


async def test_b3_explicit_revoke_stays_permanent(client, admin_client, db, world, procs, clock):
    """`hub token revoke` (без `replaced_by`) — насовсем: чистка снимает `token_revoked`, беседы не
    возвращаются ни той же сессии под новым токеном, ни новой сессии роли."""
    sub_a, c1, _c2 = await _a_with_two(client, world)
    old_id = _token_id(db, world["tok"])
    r = await admin_client.post(f"/internal/v1/admin/tokens/{old_id}/revoke", headers=_admin())
    assert r.status_code == 200, r.text
    _cleanup(db, clock)
    assert _sub_reason(db, sub_a) == "token_revoked"
    new = f32._token(db, name="mcp:all")
    body = await _sub(client, world, A, role=ROLE, token=new)
    assert "restored_channels" not in body and body["restore_source"] == sub_a and _live(db, A) == {}
    rows = {r[0]: r for r in _chan_rows(db)}
    assert rows[c1][3] == "token_revoked"


async def test_b3_same_session_resubscribes_after_core_ended_it(client, db, world, procs, clock):
    """Ядро сняло подписки живой сессии как «умершей» (`session_gone`: машина сессии «не в сети» дольше окна),
    а сессия жива и подписывается заново тем же токеном — беседы возвращаются (раньше: «та же сессия — её
    подписки и так при ней», а они уже сняты)."""
    sub_a, c1, c2 = await _a_with_two(client, world)
    now = _now(clock)

    def _gone(conn):
        subs.end_sub(conn, sub_a, "session_gone", now_ms=now, actor="core")
        for chan in (c1, c2):
            chan_subs.end(conn, chan, "session_gone", now_ms=now, actor="core")

    db.write_sync(_gone)
    clock.advance_ms(MIN_MS)
    body = await _sub(client, world, A, role=ROLE)
    assert body["created"] is True
    assert set(_restored(body)) == {world["ch"]["G1"], world["ch"]["G2"]}
    # Повтор — ничего не двоится.
    rows = _chan_rows(db)
    again = await _sub(client, world, A, role=ROLE)
    assert "restored_channels" not in again and _chan_rows(db) == rows


async def test_b3_same_session_explicit_unsubscribe_stays(client, db, world, procs, clock):
    """Та же сессия отписалась от беседы сама — повторная подписка на проект её не возвращает."""
    await _a_with_two(client, world)
    await f32._chan_end(client, world, A, "G2")
    clock.advance_ms(MIN_MS)
    body = await _sub(client, world, A, role=ROLE)
    assert "restored_channels" not in body
    assert set(_live(db, A)) == {world["ch"]["G1"]}


# --- B4: молчание ------------------------------------------------------------------------------------------


async def test_b4_role_without_source_says_so(client, db, world, procs, clock):
    body = await _sub(client, world, A, role=ROLE)
    assert body["restore_blocked"] == "no_source"
    assert "restored_channels" not in body and "restore_source" not in body
    [(result, detail)] = _audit(db, "chan_sub_restore")
    assert result == "blocked" and detail == {"sub_id": body["sub"]["id"], "blocked": "no_source"}
    # Без роли и с `restore_channels=False` — молча, как раньше.
    plain = await _sub(client, world, B, mode="messages")
    assert "restore_blocked" not in plain
    quiet = await _sub(client, world, C, role="другая", restore_channels=False)
    assert "restore_blocked" not in quiet


async def test_b4_source_with_nothing_names_source(client, db, world, procs, clock):
    sub_a = (await _sub(client, world, A, role=ROLE))["sub"]["id"]
    del procs[A[0]]
    _cleanup(db, clock)
    body = await _sub(client, world, B, role=ROLE)
    assert body["restore_source"] == sub_a and "restored_channels" not in body
    assert "restore_blocked" not in body


async def test_b4_blocked_is_audited(client, db, world, procs, clock):
    sub_a, _c1, _c2 = await _a_with_two(client, world)
    now = _now(clock)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_a, "unsubscribed", now_ms=now, actor="core"))
    del procs[A[0]]
    body = await _sub(client, world, B, role=ROLE)
    assert body["restore_blocked"] == "unsubscribed"
    result, detail = _audit(db, "chan_sub_restore")[-1]
    assert result == "blocked"
    assert detail == {"sub_id": body["sub"]["id"], "source": sub_a, "blocked": "unsubscribed"}


# --- B5: hub sub list --days N -----------------------------------------------------------------------------


async def test_b5_admin_subs_days(client, admin_client, db, world, procs, clock):
    sub_a, c1, _c2 = await _a_with_two(client, world)
    del procs[A[0]]
    _cleanup(db, clock)
    clock.advance_ms(2 * DAY_MS)

    async def ids(**params) -> tuple[set, set]:
        r = await admin_client.get("/internal/v1/admin/subs", params=params, headers=_admin())
        assert r.status_code == 200, r.text
        body = r.json()
        return {row["id"] for row in body["items"]}, {row["id"] for row in body["channels"]}

    assert await ids(all=1) == (set(), set())
    items, channels = await ids(all=1, days=3)
    assert sub_a in items and c1 in channels
    r = await admin_client.get("/internal/v1/admin/subs", params={"all": 1, "days": 3}, headers=_admin())
    row = next(row for row in r.json()["items"] if row["id"] == sub_a)
    assert row["end_reason"] == "session_gone"
    bad = await admin_client.get("/internal/v1/admin/subs", params={"all": 1, "days": 0}, headers=_admin())
    assert bad.status_code == 422


# --- ревью Ф92-1 B ------------------------------------------------------------------------------------------


async def test_review_reissue_refused_for_revoked_old_or_expired_new(
    client, admin_client, db, world, procs, clock
):
    """Явный отзыв (`hub token revoke`) не превращается в перевыпуск: `replaced_by` по уже отозванному
    токену — 422; истёкший новый токен — 422."""
    from hub.core import security

    sub_a, _c1, _c2 = await _a_with_two(client, world)
    old_id = _token_id(db, world["tok"])
    expired = db.write_sync(
        lambda conn: security.create_api_token(
            conn, name="mcp:all", kind="interactive", scopes=["read", "draft"], project_ids=None, expires_ts=1
        )
    )
    r = await admin_client.post(
        f"/internal/v1/admin/tokens/{old_id}/revoke",
        json={"replaced_by": expired.token_id},
        headers=_admin(),
    )
    assert r.status_code == 422, r.text
    assert _sub_reason(db, sub_a) is None
    r = await admin_client.post(f"/internal/v1/admin/tokens/{old_id}/revoke", headers=_admin())
    assert r.status_code == 200
    new_id = _token_id(db, f32._token(db, name="mcp:all"))
    r = await admin_client.post(
        f"/internal/v1/admin/tokens/{old_id}/revoke", json={"replaced_by": new_id}, headers=_admin()
    )
    assert r.status_code == 422, r.text
    _cleanup(db, clock)
    assert _sub_reason(db, sub_a) == "token_revoked"


async def test_review_no_role_same_session_after_core_gone_restored(client, db, world, procs, clock):
    """Сессия БЕЗ роли: ядро сняло её подписки `session_gone`, сессия жива и подписывается заново тем же
    токеном — её беседы возвращаются (источник — своя прежняя подписка той же тройки)."""
    sub = (await _sub(client, world, A, mode="messages"))["sub"]["id"]
    c1 = await _chan_sub(client, world, A, "G1")
    now = _now(clock)

    def _gone(conn):
        subs.end_sub(conn, sub, "session_gone", now_ms=now, actor="core")
        chan_subs.end(conn, c1, "session_gone", now_ms=now, actor="core")

    db.write_sync(_gone)
    clock.advance_ms(MIN_MS)
    body = await _sub(client, world, A, mode="messages")
    assert set(_restored(body)) == {world["ch"]["G1"]} and set(_live(db, A)) == {world["ch"]["G1"]}


async def test_review_blocked_audit_only_on_new_sub(client, db, world, procs, clock):
    """Отказ целиком (П7) пишется в аудит один раз — у новой подписки; повторный `hub_subscribe` — нет."""
    sub_a, _c1, _c2 = await _a_with_two(client, world)
    now = _now(clock)
    db.write_sync(lambda conn: subs.end_sub(conn, sub_a, "unsubscribed", now_ms=now, actor="core"))
    del procs[A[0]]
    first = await _sub(client, world, B, role=ROLE)
    again = await _sub(client, world, B, role=ROLE)
    assert first["restore_blocked"] == again["restore_blocked"] == "unsubscribed"
    blocked = [row[1] for row in _audit(db, "chan_sub_restore") if row[0] == "blocked"]
    # первая — `no_source` у самой первой подписки роли (A), вторая — отказ новой подписки B; повтора нет
    assert [(d["sub_id"], d["blocked"]) for d in blocked] == [
        (sub_a, "no_source"),
        (first["sub"]["id"], "unsubscribed"),
    ]


async def test_review_wide_source_skips_line_ended_for_good(client, db, world, procs, clock):
    """B2: среди свободных линий — самая свежая, которой есть что вернуть: свежая линия №4, снятая владельцем
    (`ended_by_owner`), не заслоняет более старую линию №2, кончившуюся «сама»."""
    g1 = world["ch"]["G1"]
    await _sub(client, world, A, role=ROLE, number=2)
    await _chan_sub(client, world, A, "G1")
    del procs[A[0]]
    clock.advance_ms(MIN_MS)
    _cleanup(db, clock)
    procs[D[0]] = D[1]
    sub_d = (await _sub(client, world, D, role=ROLE, number=4, restore_channels=False))["sub"]["id"]
    c_d = await _chan_sub(client, world, D, "G2")
    clock.advance_ms(MIN_MS)
    now = _now(clock)

    def _owner(conn):
        subs.end_sub(conn, sub_d, "ended_by_owner", now_ms=now, actor="admin")
        chan_subs.end(conn, c_d, "ended_by_owner", now_ms=now, actor="admin")

    db.write_sync(_owner)
    body = await _sub(client, world, C, role=ROLE, number=3)
    assert set(_restored(body)) == {g1} and "restore_blocked" not in body
