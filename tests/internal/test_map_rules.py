"""Тест 6 куска 1a фазы 1 (раздел B спеки `docs/architecture.md`): админ-ручки правил раскладки —
`match` неизвестной формы / без `project_id` / чужой (несуществующий) `account_id` -> 422/404; `audit` на
`map_rule_add`/`map_rule_rm`. Обвязка — `tests/internal/conftest.py` (тот же приём, что в `test_admin.py`)."""

import json

from .conftest import admin_headers, make_connector_account


async def _create_project(db) -> int:
    return db.write_sync(lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid)


async def test_1a_06_unknown_match_shape_is_422(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db)
    project_id = await _create_project(db)

    r = await internal_client.post(
        "/internal/v1/admin/map-rules",
        headers=admin_headers(),
        json={"account_id": account_id, "project_id": project_id, "match": {"nonsense": "x"}},
    )
    assert r.status_code == 422, r.text


async def test_1a_06_missing_project_id_is_422(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db)

    r = await internal_client.post(
        "/internal/v1/admin/map-rules",
        headers=admin_headers(),
        json={"account_id": account_id, "match": {"channel_ext_id": "c1"}},
    )
    assert r.status_code == 422, r.text


async def test_1a_06_unknown_account_is_404(internal_client, db) -> None:
    project_id = await _create_project(db)

    r = await internal_client.post(
        "/internal/v1/admin/map-rules",
        headers=admin_headers(),
        json={"account_id": 999999, "project_id": project_id, "match": {"channel_ext_id": "c1"}},
    )
    assert r.status_code == 404, r.text


async def test_1a_06_add_and_remove_write_audit(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db)
    project_id = await _create_project(db)

    r = await internal_client.post(
        "/internal/v1/admin/map-rules",
        headers=admin_headers(),
        json={
            "account_id": account_id,
            "project_id": project_id,
            "match": {"channel_ext_id": "c1", "label": "тест"},
        },
    )
    assert r.status_code == 200, r.text
    rule_id = r.json()["rule_id"]

    with db.reader() as conn:
        add_audit = conn.execute(
            "SELECT action, detail_json FROM main.audit WHERE action = 'map_rule_add'"
        ).fetchone()
    assert add_audit is not None
    detail = json.loads(add_audit[1])
    assert detail["rule_id"] == rule_id
    assert detail["account_id"] == account_id
    assert "channels_relaid" in detail

    r2 = await internal_client.delete(f"/internal/v1/admin/map-rules/{rule_id}", headers=admin_headers())
    assert r2.status_code == 200, r2.text

    with db.reader() as conn:
        rm_audit = conn.execute(
            "SELECT detail_json FROM main.audit WHERE action = 'map_rule_rm'"
        ).fetchone()
        rule_row = conn.execute("SELECT 1 FROM main.map_rule WHERE id = ?", (rule_id,)).fetchone()
    assert rm_audit is not None
    assert json.loads(rm_audit[0])["rule_id"] == rule_id
    assert rule_row is None


async def test_1a_06_remove_unknown_rule_is_404(internal_client, db) -> None:
    r = await internal_client.delete("/internal/v1/admin/map-rules/999999", headers=admin_headers())
    assert r.status_code == 404, r.text


# --- Ф14-1 A5: админ-ручка принимает kind+value (условие собирает ядро), дубль -> 409 ----------------------


async def test_f141_admin_kind_value_builds_match(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db, source="mail")
    project_id = await _create_project(db)
    base = {"account_id": account_id, "project_id": project_id}

    r = await internal_client.post(
        "/internal/v1/admin/map-rules",
        headers=admin_headers(),
        json={**base, "kind": "domain", "value": "@A.Ru"},
    )
    assert r.status_code == 200, r.text
    rules = (await internal_client.get("/internal/v1/admin/map-rules", headers=admin_headers())).json()
    assert rules[0]["match"] == {"attr": "from_domains", "has": "a.ru", "label": "отправитель @a.ru"}

    r = await internal_client.post(
        "/internal/v1/admin/map-rules", headers=admin_headers(), json={**base, "kind": "folder", "value": 1}
    )
    assert r.status_code == 422 and r.json()["detail"] == "папки есть только у Telegram"
    # и match, и kind — 422; ни того, ни другого — 422
    r = await internal_client.post(
        "/internal/v1/admin/map-rules",
        headers=admin_headers(),
        json={**base, "kind": "all", "match": {"all": True}},
    )
    assert r.status_code == 422
    r = await internal_client.post("/internal/v1/admin/map-rules", headers=admin_headers(), json=base)
    assert r.status_code == 422


async def test_f141_admin_duplicate_is_409(internal_client, db) -> None:
    account_id, _secret = make_connector_account(db, source="mail")
    project_id = await _create_project(db)
    other = await _create_project(db)
    body = {"account_id": account_id, "project_id": project_id, "kind": "label", "value": "Счета"}
    r = await internal_client.post("/internal/v1/admin/map-rules", headers=admin_headers(), json=body)
    rule_id = r.json()["rule_id"]
    r = await internal_client.post("/internal/v1/admin/map-rules", headers=admin_headers(), json=body)
    assert r.status_code == 409 and r.json()["detail"] == f"такое правило уже есть (#{rule_id})"
    # готовый match с другим label — всё равно дубль (label не часть условия)
    r = await internal_client.post(
        "/internal/v1/admin/map-rules",
        headers=admin_headers(),
        json={
            "account_id": account_id,
            "project_id": project_id,
            "match": {"attr": "labels", "has": "Счета", "label": "другое"},
        },
    )
    assert r.status_code == 409
    r = await internal_client.post(
        "/internal/v1/admin/map-rules", headers=admin_headers(), json={**body, "project_id": other}
    )
    assert r.status_code == 200


async def test_f141_admin_project_name_checks(internal_client, db) -> None:
    headers = admin_headers()
    r = await internal_client.post("/internal/v1/admin/projects", headers=headers, json={"name": "Личное"})
    assert r.status_code == 200
    r = await internal_client.post("/internal/v1/admin/projects", headers=headers, json={"name": "личное"})
    assert r.status_code == 409
    r = await internal_client.post("/internal/v1/admin/projects", headers=headers, json={"name": "Все"})
    assert r.status_code == 422
