import pytest
from httpx import ASGITransport, AsyncClient

from hub.core import security
from hub.core.app_public import create_public_app
from hub.core.db import db_of, next_seq
from hub.core.security import compute_csrf

PORT = 8787
BASE_URL = f"http://127.0.0.1:{PORT}"
ORIGIN = BASE_URL


class FakeClock:
    """Часы приложения — секунды, как `time.time` (см. create_public_app). Тесты сами их двигают."""

    def __init__(self, start_s: float = 1_700_000_000.0) -> None:
        self.value = start_s

    def __call__(self) -> float:
        return self.value

    def advance_ms(self, ms: int) -> None:
        self.value += ms / 1000


@pytest.fixture(autouse=True)
def _legacy_runs_scope(monkeypatch):
    """Ф57-2: скоупы `runs` (дежурная) и `orchestrate` (чат оркестратора) новым токенам не выдаются (нет в
    `BEARER_SCOPES`), но у старых токенов они остались в базе. Тесты на «старый токен» заводят такие строки
    через `create_api_token` — разрешаем скоупы только внутри тестов."""
    monkeypatch.setattr(security, "BEARER_SCOPES", security.BEARER_SCOPES | {"runs", "orchestrate"})


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(db, clock):
    return create_public_app(db, clock=clock, port=PORT)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as c:
        yield c


def csrf_headers(cookie_value: str) -> dict[str, str]:
    """Origin + X-Hub-CSRF для не-GET запроса под валидной cookie-сессией."""
    return {"Origin": ORIGIN, "X-Hub-CSRF": compute_csrf(cookie_value)}


# --- Помощники для наполнения БД в тестах API (стиль — tests/core/test_outbox.py) ------------------------


def insert_project(db, *, name="P", color=None, sort=0, muted=0, workdir=None):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                "INSERT INTO main.project (name, color, sort, muted, workdir) VALUES (?, ?, ?, ?, ?)",
                (name, color, sort, muted, workdir),
            ).lastrowid
        )
    )


def insert_account(db, *, source="tg", db_name="main"):
    def _fn(conn):
        cur = conn.execute("INSERT INTO main.account (source, db) VALUES (?, ?)", (source, db_name))
        return cur.lastrowid

    return db.write_sync(_fn)


def insert_channel(
    db,
    account_id,
    *,
    project_id=None,
    ext_id="c1",
    kind="dm",
    db_name="main",
    title=None,
    tier=None,
    muted=0,
    archived=0,
    map_reason=None,
    last_msg_ts=None,
    my_msg_count=0,
    first_seen_ts=0,
    last_read_seq=None,
    parent_channel_id=None,
):
    return db.write_sync(
        lambda conn: (
            conn.execute(
                f"INSERT INTO {db_name}.channel (account_id, ext_id, kind, project_id, title, tier, muted, "
                "archived, map_reason, last_msg_ts, my_msg_count, first_seen_ts, last_read_seq, "
                "parent_channel_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    ext_id,
                    kind,
                    project_id,
                    title,
                    tier,
                    muted,
                    archived,
                    map_reason,
                    last_msg_ts,
                    my_msg_count,
                    first_seen_ts,
                    last_read_seq,
                    parent_channel_id,
                ),
            ).lastrowid
        )
    )


def _ext_sort_for(ext_id: str | None) -> int:
    """Та же формула, что ingest._ext_sort, но терпит None (заготовка outbox — см. схему, DEFAULT 0)."""
    if ext_id is not None and ext_id.isascii() and ext_id.isdigit() and len(ext_id) <= 18:
        return int(ext_id)
    return 0


def insert_message(
    db,
    channel_id,
    *,
    ext_id,
    ts,
    text="hi",
    is_me=0,
    kind="text",
    deleted=0,
    outbox_id=None,
    sender_contact_id=None,
    ext_sort=None,
    # Спека 2b (треды Slack, docs/architecture.md): значения по умолчанию совпадают со
    # схемой (DEFAULT 0) — существующие вызовы без этих kwargs не меняют поведение.
    thread_ext_id=None,
    thread_broadcast=0,
    mentions_me=0,
    purged=0,
):
    schema = db_of(channel_id)
    if ext_sort is None:
        ext_sort = _ext_sort_for(ext_id)

    def _fn(conn):
        seq = next_seq(conn)
        cur = conn.execute(
            f"INSERT INTO {schema}.message (seq, first_seq, account_id, channel_id, ext_id, ext_sort, ts, "
            "is_me, kind, text, deleted, outbox_id, sender_contact_id, thread_ext_id, thread_broadcast, "
            "mentions_me, purged) VALUES (?, ?, "
            f"(SELECT account_id FROM {schema}.channel WHERE id = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?)",
            (
                seq,
                seq,
                channel_id,
                channel_id,
                ext_id,
                ext_sort,
                ts,
                is_me,
                kind,
                text,
                deleted,
                outbox_id,
                sender_contact_id,
                thread_ext_id,
                thread_broadcast,
                mentions_me,
                purged,
            ),
        )
        return cur.lastrowid

    return db.write_sync(_fn)


def insert_thread(
    db,
    channel_id,
    *,
    ext_id,
    last_reply_ts=None,
    subscribed=0,
    source_reply_count=None,
    source_latest_reply_ext=None,
    source_subscribed=None,
    last_read_ts=None,
    last_read_sort=None,
    last_read_ext=None,
    first_seen_ts=0,
):
    """Строка `thread` напрямую — спека 2b (тесты API тредов не всегда идут через ingest)."""
    schema = db_of(channel_id)

    def _fn(conn):
        cur = conn.execute(
            f"INSERT INTO {schema}.thread (channel_id, ext_id, last_reply_ts, subscribed, "
            "source_reply_count, source_latest_reply_ext, source_subscribed, last_read_ts, last_read_sort, "
            "last_read_ext, first_seen_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel_id,
                ext_id,
                last_reply_ts,
                subscribed,
                source_reply_count,
                source_latest_reply_ext,
                source_subscribed,
                last_read_ts,
                last_read_sort,
                last_read_ext,
                first_seen_ts,
            ),
        )
        return cur.lastrowid

    return db.write_sync(_fn)


def make_bearer_token(
    db, *, kind="interactive", scopes=("read",), project_ids=None, name="bearer", expires_ts=None
):
    return db.write_sync(
        lambda conn: security.create_api_token(
            conn, name=name, kind=kind, scopes=list(scopes), project_ids=project_ids, expires_ts=expires_ts
        )
    ).token


async def login_via_code(client, db, clock) -> tuple[str, str]:
    """Полный вход: заводит одноразовый код и меняет его на cookie-сессию. Возвращает (cookie, csrf)."""
    now_ms = int(clock() * 1000)
    code = db.write_sync(lambda conn: security.create_login_code(conn, now_ms=now_ms))
    r = await client.post("/api/v1/auth/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    cookie = client.cookies.get("hub_session")
    assert cookie is not None
    return cookie, r.json()["csrf"]
