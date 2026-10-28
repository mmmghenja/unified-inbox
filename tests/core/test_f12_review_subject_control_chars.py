"""Ядро пропускает тему с U+2028/NEL/VT (проверяет только \\r и \\n), а сборка письма на них падает
ValueError (проверено ниже) -> send() без результата -> каркас ставит `unknown` (base.py `except Exception`):
письмо не уходило, а владелец видит «исход неизвестен». Ожидание: ядро отвечает 422 (Invalid) сразу."""

import json

import pytest

from hub.connectors.mail import convert
from hub.core import new_recipient


@pytest.fixture
def env(db):
    """Стенд теста ревьюера (`.runs/critic-f12-1/conftest.py`): два проекта, Telegram, почта, токен."""

    def _fn(conn):
        a = conn.execute("INSERT INTO main.project (name) VALUES ('IIP')").lastrowid
        b = conn.execute("INSERT INTO main.project (name) VALUES ('Initech')").lastrowid
        tg = conn.execute(
            "INSERT INTO main.account (source, db, config_json) VALUES ('telegram', 'main', ?)",
            (json.dumps({"idempotent_send": True}),),
        ).lastrowid
        mail = conn.execute(
            "INSERT INTO main.account (source, db, config_json) VALUES ('mail', 'main', ?)",
            (json.dumps({"address": "me@gmail.com"}),),
        ).lastrowid
        tok = conn.execute(
            "INSERT INTO main.api_token (name, kind, hash) VALUES ('mcp:iip', 'interactive', 'x')"
        ).lastrowid
        return {"a": a, "b": b, "tg": tg, "mail": mail, "tok": tok}

    return db.write_sync(_fn)


@pytest.mark.parametrize("sep", [" ", "\x85", "\x0b"])
def test_letter_builder_rejects(sep):
    with pytest.raises(ValueError):
        convert.build_letter(
            recipients=["client@example.com"],
            subject=f"Подпись{sep}для ООО",
            text="t",
            address="me@gmail.com",
            display_name="Me",
            message_id="<hub-x@gmail.com>",
            now_ts=0,
        )


@pytest.mark.parametrize("sep", [" ", "\x85", "\x0b"])
def test_core_rejects_subject_that_letter_builder_rejects(db, env, sep):
    with pytest.raises(new_recipient.Invalid):
        db.write_sync(
            lambda conn: new_recipient.create_draft(
                conn,
                account_id=env["mail"],
                project_id=env["a"],
                to="client@example.com",
                text="Добрый день",
                subject=f"Подпись{sep}для ООО",
                from_address=None,
                api_token_id=env["tok"],
                idempotency_key=None,
                now_ms=1_000,
            )
        )
