# Спека 1d-1, п.5: `GET /projects/{id}/channels` подписывает топик (`kind='topic'`) родительским названием
# форума (`parent_title`) и НЕ показывает сам форум, если у него есть дочерние топики.
from .conftest import insert_account, insert_channel, insert_project, login_via_code, make_bearer_token


async def test_topic_has_parent_title_and_forum_with_children_is_hidden(client, db, clock):
    acc = insert_account(db)
    proj = insert_project(db)
    forum = insert_channel(db, acc, project_id=proj, ext_id="forum1", kind="group", title="Мой форум")
    topic = insert_channel(
        db,
        acc,
        project_id=proj,
        ext_id="topic1",
        kind="topic",
        title="Обсуждение",
        parent_channel_id=forum,
    )
    plain = insert_channel(db, acc, project_id=proj, ext_id="plain1", kind="dm", title="Обычный чат")

    await login_via_code(client, db, clock)

    r = await client.get(f"/api/v1/projects/{proj}/channels")
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.json()}

    assert forum not in by_id  # форум с дочерним топиком в списке не показывается
    assert by_id[topic]["kind"] == "topic"
    assert by_id[topic]["parent_title"] == "Мой форум"
    assert by_id[plain]["parent_title"] is None  # не-топик — всегда NULL


async def test_forum_without_children_still_shown_with_null_parent_title(client, db, clock):
    """Обычная группа (без топиков) — не скрывается и `parent_title` у неё NULL (она не топик)."""
    acc = insert_account(db)
    proj = insert_project(db)
    group = insert_channel(db, acc, project_id=proj, ext_id="grp1", kind="group", title="Просто группа")

    await login_via_code(client, db, clock)

    r = await client.get(f"/api/v1/projects/{proj}/channels")
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.json()}
    assert group in by_id
    assert by_id[group]["parent_title"] is None


async def test_topic_listing_respects_project_scope_of_other_token(client, db):
    """Токен, скоупленный на чужой проект, не видит ни топик, ни форум — весь проект целиком 404."""
    acc = insert_account(db)
    proj_a = insert_project(db, name="A")
    proj_b = insert_project(db, name="B")
    forum = insert_channel(db, acc, project_id=proj_a, ext_id="forum1", kind="group", title="Форум A")
    insert_channel(
        db,
        acc,
        project_id=proj_a,
        ext_id="topic1",
        kind="topic",
        title="Тема",
        parent_channel_id=forum,
    )
    insert_channel(db, acc, project_id=proj_b, ext_id="b1", kind="dm", title="Чужой канал")

    token = make_bearer_token(db, project_ids=[proj_b])
    headers = {"Authorization": f"Bearer {token}"}

    r = await client.get(f"/api/v1/projects/{proj_a}/channels", headers=headers)
    assert r.status_code == 404

    r2 = await client.get(f"/api/v1/projects/{proj_b}/channels", headers=headers)
    assert r2.status_code == 200
    titles = [c["title"] for c in r2.json()]
    assert titles == ["Чужой канал"]
