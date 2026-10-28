"""Ф14-2a, коннектор Telegram (`docs/architecture.md`, тесты F 9–11): новая беседа
по id пользователя (`tg_user`), файлы и альбом в новую беседу на разрешённый peer, публикация контактов
`tg_contacts` порциями. Ядро — подменное (`FakeFileCore`), Telegram — `FakeGateway` в памяти. Сети нет.
"""

from __future__ import annotations

import json

import pytest
from telethon import errors
from telethon.tl import types

from hub.connectors import meta_parts
from hub.connectors.telegram.connector import TelegramConnector, contacts_items
from hub.connectors.telegram.gateway import contacts_hash

from ..outbox_file_fakes import PDF_BYTES, PNG_BYTES, FakeFileCore, file_meta
from .helpers import ME_ID, FakeGateway, make_message, make_user

NOW_MS = 1_700_000_000_000
USER_ID = 555
TOKEN = "tok-new"
PNG2 = PNG_BYTES + b"-2"
PNG3 = PNG_BYTES + b"-3"


class _Mono:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _setup(tmp_path, *, data=None):
    core = FakeFileCore()
    core.files[7] = data if data is not None else [PNG_BYTES, PNG2, PNG3]
    gw = FakeGateway()
    gw.now_ms = NOW_MS
    mono = _Mono()
    connector = TelegramConnector(
        core.client(), gw, account_id=1, media_tmp_dir=tmp_path / "tmp", wall=lambda: NOW_MS / 1000, mono=mono
    )
    connector._me_id = ME_ID  # noqa: SLF001
    return connector, gw, core, mono


def _lease(new_to: dict, *, files=None, text="Здравствуйте", maybe_sent=False, **extra) -> dict:
    lease = {
        "outbox_id": 7,
        "lease_gen": 1,
        "channel_ext_id": "new:" + "a" * 32,
        "text": text,
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": TOKEN,
        "mode": "send",
        "send_started_ts": NOW_MS,
        "attempts": 1,
        "maybe_sent": maybe_sent,
        "known_own_ext_ids": [],
        "files": files,
        "new_to": new_to,
    }
    lease.update(extra)
    return lease


def _tg_user(**kw) -> dict:
    return {"kind": "tg_user", "user_id": USER_ID, "name": "Пётр", "username": None, **kw}


def _photos(n=3) -> list[dict]:
    return [file_meta(d, name=f"{i}.png") for i, d in enumerate([PNG_BYTES, PNG2, PNG3][:n])]


# --- тест 9: `tg_user` --------------------------------------------------------------------------------------


async def test_9_cached_user_sends_text(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.seed_peer(USER_ID, types.InputPeerUser(USER_ID, 0))
    result = await connector.send(_lease(_tg_user()))
    assert (result.outcome, result.peer_ext_id, result.peer_title) == ("sent", "555", "Пётр")
    (sent,) = gw.sent
    assert sent["marked_id"] == USER_ID and sent["text"] == "Здравствуйте"
    assert result.ext_id == sent["ext_id"]
    assert gw.resolve_calls == [] and gw.contacts_calls == [] and gw.iter_dialogs_calls == 0


async def test_9_miss_with_username_resolves_same_user(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.add_username("petr_i", make_user(USER_ID, first_name="Пётр", username="petr_i"))
    result = await connector.send(_lease(_tg_user(username="petr_i")))
    assert (result.outcome, result.peer_ext_id) == ("sent", "555")
    assert gw.resolve_calls == ["petr_i"] and gw.contacts_calls == []
    assert gw.sent[0]["marked_id"] == USER_ID


async def test_9_username_of_other_person_is_permanent(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.add_username("petr_i", make_user(777, first_name="Не тот", username="petr_i"))
    result = await connector.send(_lease(_tg_user(username="petr_i")))
    assert result.outcome == "permanent" and "другого человека" in result.error
    assert gw.sent == [] and gw.send_calls == 0


async def test_9_miss_without_username_get_contacts_then_cache(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.contacts = [make_user(USER_ID, first_name="Пётр", contact=True)]
    result = await connector.send(_lease(_tg_user()))
    assert (result.outcome, result.peer_ext_id) == ("sent", "555")
    assert gw.contacts_calls == [0]  # ровно один GetContacts(hash=0)
    assert gw.get_input_entity_calls == [USER_ID, USER_ID]


async def test_9_contact_not_found_is_permanent(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    result = await connector.send(_lease(_tg_user()))
    assert (result.outcome, result.error) == ("permanent", "контакт не найден — напишите по @username")
    assert gw.contacts_calls == [0] and gw.send_calls == 0


@pytest.mark.parametrize("where", ["contacts", "username"])
async def test_9_flood_while_resolving_is_temporary_without_send(tmp_path, where) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    flood = errors.FloodWaitError(request=None, capture=30)
    if where == "contacts":
        gw.contacts_faults.append(flood)
        new_to = _tg_user()
    else:
        gw.resolve_faults.append(flood)
        new_to = _tg_user(username="petr_i")
    result = await connector.send(_lease(new_to))
    assert (result.outcome, result.retry_after_ms) == ("temporary", 30_000)
    assert gw.send_calls == 0 and gw.sent == []


async def test_9_network_while_resolving_is_temporary(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.contacts_faults.append(ConnectionError("сеть"))
    result = await connector.send(_lease(_tg_user()))
    assert (result.outcome, result.error) == ("temporary", "network")
    assert gw.send_calls == 0


async def test_9_privacy_text_for_tg_user(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.seed_peer(USER_ID, types.InputPeerUser(USER_ID, 0))
    gw.queue_send_faults(errors.UserPrivacyRestrictedError(request=None))
    result = await connector.send(_lease(_tg_user()))
    assert (result.outcome, result.error) == (
        "permanent",
        "человек принимает личку только от контактов/Premium",
    )


@pytest.mark.parametrize(
    "new_to", [{"kind": "tg_user", "user_id": 0}, {"kind": "tg_user", "user_id": "5"}, {"kind": "tg_phone"}]
)
async def test_9_bad_new_to_is_permanent(tmp_path, new_to) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    result = await connector.send(_lease(new_to))
    assert result.outcome == "permanent"
    assert gw.send_calls == 0 and gw.get_input_entity_calls == []


async def test_9_reconcile_tg_user(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.seed_peer(USER_ID, types.InputPeerUser(USER_ID, 0))
    gw.add_message(USER_ID, make_message(900, types.PeerUser(USER_ID), message="Здравствуйте", out=True))
    found = await connector.reconcile(_lease(_tg_user(), mode="reconcile"))
    assert (found.outcome, found.ext_id, found.peer_ext_id, found.peer_title) == (
        "sent",
        "900",
        "555",
        "Пётр",
    )
    missing = await connector.reconcile(_lease(_tg_user(user_id=556), mode="reconcile"))
    assert missing.outcome == "unknown"
    assert gw.send_calls == 0


# --- тест 10: файл и альбом в новую беседу ------------------------------------------------------------------


def _new_tos():
    return [
        {"kind": "tg_username", "username": "Petr_Ivanov"},
        _tg_user(),
    ]


def _prepare_peer(gw, new_to):
    user = make_user(USER_ID, first_name="Пётр", username="Petr_Ivanov")
    if new_to["kind"] == "tg_username":
        gw.add_username("Petr_Ivanov", user)
    else:
        gw.seed_peer(USER_ID, types.InputPeerUser(USER_ID, 0))


@pytest.mark.parametrize("new_to", _new_tos(), ids=["tg_username", "tg_user"])
async def test_10_file_to_new_chat(tmp_path, new_to) -> None:
    connector, gw, core, _mono = _setup(tmp_path, data=PDF_BYTES)
    _prepare_peer(gw, new_to)
    file = file_meta(PDF_BYTES, kind="document", mime="application/pdf", name="акт.pdf")
    result = await connector.send(_lease(new_to, files=[file], text="Акт"))
    (call,) = gw.media_calls
    assert call["marked_id"] == USER_ID and call["data"] == PDF_BYTES and call["text"] == "Акт"
    assert (result.outcome, result.peer_ext_id, result.ext_id) == ("sent", "555", gw.sent[0]["ext_id"])
    assert result.peer_title == "Пётр"
    assert [f["idx"] for f in core.fetches] == ["0"]


@pytest.mark.parametrize("new_to", _new_tos(), ids=["tg_username", "tg_user"])
async def test_10_album_to_new_chat(tmp_path, new_to) -> None:
    connector, gw, core, _mono = _setup(tmp_path)
    _prepare_peer(gw, new_to)
    result = await connector.send(_lease(new_to, files=_photos(), text="Фото"))
    (call,) = gw.album_calls
    assert call["marked_id"] == USER_ID and call["text"] == "Фото"
    assert [i["data"] for i in call["items"]] == [PNG_BYTES, PNG2, PNG3]
    assert result.outcome == "sent" and result.peer_ext_id == "555"
    assert result.ext_ids == tuple(s["ext_id"] for s in gw.sent)
    assert gw.media_calls == []


@pytest.mark.parametrize("new_to", _new_tos(), ids=["tg_username", "tg_user"])
async def test_10_maybe_sent_searches_resolved_peer_without_send(tmp_path, new_to) -> None:
    connector, gw, core, _mono = _setup(tmp_path)
    _prepare_peer(gw, new_to)
    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
    for k in range(3):
        gw.add_message(
            USER_ID,
            make_message(
                700 + k,
                types.PeerUser(USER_ID),
                message="Фото" if k == 0 else "",
                out=True,
                date_ms=NOW_MS,
                media=media,
                grouped_id=9,
            ),
        )
    result = await connector.send(_lease(new_to, files=_photos(), text="Фото", maybe_sent=True))
    assert (result.outcome, result.ext_ids, result.peer_ext_id) == ("sent", ("700", "701", "702"), "555")
    assert gw.album_calls == [] and core.fetches == []
    assert gw.get_messages_calls and all(c[0] == USER_ID for c in gw.get_messages_calls)

    # один файл: поиск своего сообщения с медиа — тоже в истории разрешённого peer
    gw.add_message(USER_ID, make_message(800, types.PeerUser(USER_ID), message="Акт", out=True, media=media))
    file = file_meta(PNG_BYTES, name="a.png")
    one = await connector.send(_lease(new_to, files=[file], text="Акт", maybe_sent=True))
    assert (one.outcome, one.ext_id, one.peer_ext_id) == ("sent", "800", "555")
    assert gw.media_calls == []


async def test_10_caption_and_mixed_album_checked_before_resolving(tmp_path) -> None:
    connector, gw, core, _mono = _setup(tmp_path)
    long_caption = await connector.send(_lease(_tg_user(), files=_photos(1), text="я" * 1025))
    assert (long_caption.outcome, long_caption.error) == ("permanent", "подпись длиннее 1024")
    mixed = [file_meta(PNG_BYTES, name="a.png"), file_meta(PDF_BYTES, kind="document", name="b.pdf")]
    result = await connector.send(_lease(_tg_user(), files=mixed))
    assert result.outcome == "permanent" and "смешанный" in result.error
    assert gw.get_input_entity_calls == [] and core.fetches == []


# --- тест 11: контакты ------------------------------------------------------------------------------------


def _contacts_users() -> list:
    return [
        make_user(ME_ID, first_name="владелец", is_self=True, contact=True),
        make_user(20, first_name="Олег", last_name="Петров", username="oleg_p", contact=True),
        make_user(10, first_name="Анна", contact=True),
        make_user(30, first_name="Бот", username="some_bot", bot=True, contact=True),
        types.User(id=40, deleted=True, contact=True, access_hash=0),
    ]


def _meta(connector) -> list[dict]:
    return [e for e in connector._event_buffer if e["type"] == "account_meta"]  # noqa: SLF001


async def test_11_contacts_published_without_phones_bots_deleted_me(tmp_path) -> None:
    connector, gw, _core, mono = _setup(tmp_path)
    users = _contacts_users()
    users[1] = types.User(
        id=20,
        contact=True,
        access_hash=0,
        first_name="Олег",
        last_name="Петров",
        username="oleg_p",
        phone="79000001122",
    )
    gw.contacts = users
    await connector._maybe_refresh_contacts()  # noqa: SLF001
    events = _meta(connector)
    assert [e["key"] for e in events] == ["tg_contacts:0", "tg_contacts"]
    assert events[0]["value"] == [
        {"id": 10, "first": "Анна", "last": None, "username": None},
        {"id": 20, "first": "Олег", "last": "Петров", "username": "oleg_p"},
    ]
    assert events[1]["value"] == {"parts": 1, "count": 2}
    assert "7999" not in json.dumps(events)

    # через час: `ContactsNotModified` — без публикации
    connector._event_buffer.clear()  # noqa: SLF001
    await connector._maybe_refresh_contacts()  # noqa: SLF001 — раньше часа: не спрашивает
    assert gw.contacts_calls == [0]
    mono.value += 3601
    await connector._maybe_refresh_contacts()  # noqa: SLF001
    assert gw.contacts_calls == [0, gw.contacts_hash_now()]
    assert _meta(connector) == []

    # список изменился — публикуется заново
    gw.contacts.append(make_user(50, first_name="Вера", contact=True))
    mono.value += 3601
    await connector._maybe_refresh_contacts()  # noqa: SLF001
    assert [len(e["value"]) for e in _meta(connector) if e["key"] == "tg_contacts:0"] == [3]


async def test_11_contacts_failure_logged_without_names(tmp_path, caplog) -> None:
    connector, gw, _core, mono = _setup(tmp_path)
    gw.contacts_faults.append(errors.FloodWaitError(request=None, capture=10))
    await connector._maybe_refresh_contacts()  # noqa: SLF001
    assert _meta(connector) == []
    assert "контакты не получены (FloodWaitError)" in caplog.text
    gw.contacts = [make_user(10, first_name="Анна", contact=True)]
    await connector._maybe_refresh_contacts()  # noqa: SLF001 — повтор не раньше часа
    assert gw.contacts_calls == [0]
    mono.value += 3601
    await connector._maybe_refresh_contacts()  # noqa: SLF001
    assert [e["key"] for e in _meta(connector)] == ["tg_contacts:0", "tg_contacts"]


async def test_11_many_contacts_in_parts_header_last(tmp_path) -> None:
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.contacts = [
        make_user(
            1000 + i, first_name=f"Имя{i}", last_name="Фамилия" * 3, username=f"user_{i:05d}", contact=True
        )
        for i in range(1500)
    ]
    await connector._maybe_refresh_contacts()  # noqa: SLF001
    events = _meta(connector)
    assert events[-1]["key"] == "tg_contacts"
    parts = events[:-1]
    assert len(parts) >= 3
    assert [e["key"] for e in parts] == [f"tg_contacts:{i}" for i in range(len(parts))]
    for event in parts:
        size = len(json.dumps(event["value"], ensure_ascii=False, sort_keys=True).encode())
        assert size <= meta_parts.PART_MAX_BYTES
    assert events[-1]["value"] == {"parts": len(parts), "count": 1500}
    assert sum(len(e["value"]) for e in parts) == 1500


async def test_11_contacts_in_sync(tmp_path) -> None:
    """Сверка на старте публикует контакты (обычная сверка зовёт `_maybe_refresh_contacts`)."""
    connector, gw, _core, _mono = _setup(tmp_path)
    gw.contacts = [make_user(10, first_name="Анна", contact=True)]

    async def _heads():
        return []

    connector._client.channels_heads = _heads  # noqa: SLF001
    await connector._sync_impl("start")  # noqa: SLF001
    assert [e["key"] for e in _meta(connector) if e["key"].startswith("tg_contacts")] == [
        "tg_contacts:0",
        "tg_contacts",
    ]


def test_11_contacts_items_and_hash() -> None:
    items = contacts_items(_contacts_users(), me_id=ME_ID)
    assert [i["id"] for i in items] == [10, 20]
    assert contacts_hash(2, [20, 10]) == contacts_hash(2, [10, 20])
    assert contacts_hash(2, [10, 20]) != contacts_hash(3, [10, 20])
    assert -(2**63) <= contacts_hash(5, [2**40, 2**52]) < 2**63


def test_meta_parts_split() -> None:
    items = [{"id": i, "name": "x" * 100} for i in range(1000)]
    events = meta_parts.meta_events("slack_people", items, max_bytes=10 * 1024)
    assert events[-1] == {
        "type": "account_meta",
        "key": "slack_people",
        "value": {"parts": len(events) - 1, "count": 1000},
    }
    assert [i for e in events[:-1] for i in e["value"]] == items
    for event in events[:-1]:
        assert len(json.dumps(event["value"], ensure_ascii=False, sort_keys=True).encode()) <= 10 * 1024
    assert meta_parts.meta_events("x", []) == [
        {"type": "account_meta", "key": "x", "value": {"parts": 0, "count": 0}}
    ]
