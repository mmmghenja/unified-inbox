"""Ф62-6, правки после ревью (docs/architecture.md → «Правки после ревью»;
воспроизведения ревьюера — `.runs/critic-f626/test_repro_f626.py`, здесь утверждения перевёрнуты):

1. номер, записанный jid'ом (`79…@s.whatsapp.net`), которого нет среди личек аккаунта, — это номер: в заявке
   `kind="phone"` с маской, в задании коннектору `phone` (предел `wa_phone_daily`); `@lid` без лички — отказ;
2. вторая открытая заявка `create` с тем же названием на тот же аккаунт — 409; задание сверки П1 несёт
   `exclude` — группы прочих заявок `create` и беседы хаба с тем же названием, бывшие до первой выдачи;
3. `#id` лички вне видимости токена сессии или без проекта — «не найдена», без имени; владелец из интерфейса —
   без ограничения."""

from .conftest import insert_channel
from .test_f62_group_ops import (  # noqa: F401 — фикстуры
    API,
    NEW_GROUP,
    _create_body,
    _lease,
    _op,
    _report,
    _token,
    ic,
    internal_app,
    procs,
    ui,
    wa,
    world,
)

__all__ = ["ic", "internal_app", "procs", "ui", "wa", "world"]  # фикстуры из test_f62_group_ops

GROUP = "120363000000000001@g.us"  # группа «hub test» фикстуры `wa`


# --- 1. номерной jid — как номер ----------------------------------------------------------------------------


async def test_unknown_number_jid_is_parsed_as_phone(client, ui, db, clock, world, wa, ic):
    w = world
    made = await _op(
        client,
        w,
        {"kind": "add", "channel_id": wa["group"], "participants": ["79000002233@s.whatsapp.net"]},
    )
    [person] = made["participants"]
    assert person["kind"] == "phone" and person["mask"] and "79000002233" not in str(person)
    r = await ui.post(f"{API}/group-ops/{made['id']}/approve")
    assert r.status_code == 200, r.text
    [job] = await _lease(ic, wa)
    assert job["participants"] == [{"i": 0, "jid": None, "phone": "+79000002233"}]  # под пределом номеров


async def test_known_dm_jid_stays_jid_and_unknown_lid_refused(client, db, clock, world, wa):
    w = world
    made = await _op(
        client,
        w,
        {"kind": "add", "channel_id": wa["group"], "participants": ["79000001122@s.whatsapp.net"]},
    )
    assert made["participants"][0]["kind"] == "jid"  # личка «Пётр» есть у аккаунта
    r = await _op(
        client, w, {"kind": "add", "channel_id": wa["group"], "participants": ["123456789@lid"]}, expect=422
    )
    assert "@lid" in r["detail"]
    insert_channel(db, wa["acc"], project_id=w["p3"], ext_id="123456789@lid", kind="dm")
    made = await _op(client, w, {"kind": "add", "channel_id": wa["group"], "participants": ["123456789@lid"]})
    assert made["participants"][0]["kind"] == "jid"


# --- 2. одно название — одна открытая заявка create; exclude сверки -----------------------------------------


async def test_second_open_create_with_same_title_is_409(client, ui, db, clock, world, wa):
    w = world
    await _op(client, w, _create_body(w))
    r = await _op(client, w, _create_body(w), expect=409)
    assert "hub test 2" in r["detail"] and "уже ждёт создания" in r["detail"]
    r = await ui.post(
        f"{API}/group-ops/ui", {"kind": "create", "title": "hub test 2", "participants": ["+79000001133"]}
    )
    assert r.status_code == 409, r.text
    await _op(client, w, _create_body(w, title="hub test 5"))  # другое название — можно


async def test_reconcile_job_excludes_sibling_and_old_groups(client, ui, db, clock, world, wa, ic):
    w = world
    first = await _op(client, w, _create_body(w, title="hub test"))
    await ui.post(f"{API}/group-ops/{first['id']}/approve")
    [job] = await _lease(ic, wa)
    # Беседа «hub test» фикстуры была в хабе до первой выдачи — сверке её не брать.
    assert job["exclude"] == [GROUP]
    await _report(ic, wa, job, result={"chat": NEW_GROUP, "participants": []})
    clock.advance_ms(1000)
    second = await _op(client, w, _create_body(w, title="hub test"))  # первая закрыта — можно
    await ui.post(f"{API}/group-ops/{second['id']}/approve")
    clock.advance_ms(120_000)
    [job2] = await _lease(ic, wa)
    assert job2["id"] == second["id"]
    assert job2["exclude"] == sorted([GROUP, NEW_GROUP])  # группа соседней заявки — тоже


# --- 3. #id лички — только видимая токену ------------------------------------------------------------------


async def test_dm_ref_outside_token_scope_is_not_found_without_name(client, ui, db, clock, world, wa):
    w = world
    foreign = insert_channel(
        db,
        wa["acc"],
        project_id=w["p4"],
        ext_id="79000004433@s.whatsapp.net",
        kind="dm",
        title="Секретный контакт",
    )
    loose = insert_channel(
        db,
        wa["acc"],
        project_id=None,
        ext_id="79000004400@s.whatsapp.net",
        kind="dm",
        title="Личное без проекта",
    )
    scoped, _ = _token(db, name="mcp:iip", project_ids=[w["p3"]])
    for ref in (foreign, loose):
        r = await _op(client, w, _create_body(w, participants=[f"#{ref}"]), token=scoped, expect=422)
        assert r["detail"] == f"participants[0]: беседа #{ref} не найдена"
    r = await _op(client, w, _create_body(w, participants=["#999999"]), token=scoped, expect=422)
    assert r["detail"] == "participants[0]: беседа #999999 не найдена"  # тот же отказ, что у несуществующей
    made = await _op(client, w, _create_body(w, participants=[f"#{wa['dm']}"]), token=scoped)
    assert made["participants"][0]["name"] == "Пётр"  # своя личка — можно
    r = await ui.post(
        f"{API}/group-ops/ui",
        {"kind": "create", "title": "hub test 6", "participants": [f"#{foreign}", f"#{loose}"]},
    )
    assert r.status_code == 201, r.text  # владелец из интерфейса — без ограничения
