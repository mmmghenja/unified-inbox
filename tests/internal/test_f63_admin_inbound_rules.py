"""Ф63-1: админ-ручки `/admin/inbound-rules` (`hub rules list`, `hub rules add`, `hub rules rm`)."""

from hub.core import inbound_rules

from .conftest import admin_headers


async def test_admin_list_inbound_rules(internal_client, db) -> None:
    project_id = db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
    )
    r = await internal_client.get(
        "/internal/v1/admin/inbound-rules", headers=admin_headers(), params={"project_id": project_id}
    )
    assert r.status_code == 200 and r.json() == []
    actor = {"kind": "ui", "token_id": None, "request_id": "t"}
    db.write_sync(
        lambda conn: inbound_rules.add(
            conn, project_id, {"title": "т", "text": ["заказ"], "wake": ["менеджер"]}, actor=actor, now_ms=1
        )
    )
    r = await internal_client.get(
        "/internal/v1/admin/inbound-rules", headers=admin_headers(), params={"project_id": project_id}
    )
    [rule] = r.json()
    assert rule["title"] == "т" and rule["wake_status"] == [{"role": "менеджер", "listening": "none"}]
    r = await internal_client.get(
        "/internal/v1/admin/inbound-rules", headers=admin_headers(), params={"project_id": 9999}
    )
    assert r.status_code == 404


async def test_admin_add_and_remove_archive_rule(internal_client, db) -> None:
    project_id = db.write_sync(
        lambda conn: conn.execute("INSERT INTO main.project (name) VALUES ('P')").lastrowid
    )
    url = "/internal/v1/admin/inbound-rules"
    body = {"title": "тест", "from": ["initech.example.com"], "subject": ["Order Notification"], "archive": True}
    r = await internal_client.post(url, headers=admin_headers(), params={"project_id": project_id}, json=body)
    assert r.status_code == 200, r.text
    rule = r.json()
    assert rule["action"]["archive"] is True and "сразу в архив" in rule["description"]
    r = await internal_client.post(
        url, headers=admin_headers(), params={"project_id": project_id}, json={"title": "т"}
    )
    assert r.status_code == 422
    r = await internal_client.post(url, headers=admin_headers(), params={"project_id": project_id}, json=[1])
    assert r.status_code == 422
    r = await internal_client.delete(
        f"{url}/{rule['id']}", headers=admin_headers(), params={"project_id": project_id}
    )
    assert r.status_code == 200 and r.json() == {"id": rule["id"], "removed": True}
    r = await internal_client.delete(
        f"{url}/{rule['id']}", headers=admin_headers(), params={"project_id": project_id}
    )
    assert r.status_code == 404
