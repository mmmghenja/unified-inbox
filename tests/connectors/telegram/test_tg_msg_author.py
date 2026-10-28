"""Ф46-1 (`docs/architecture.md`), коннектор Telegram: первое
сообщение автору сообщения группы — кэш сессии -> доступ через сообщение (`users.GetUsers` с
`InputUserFromMessage`): полный пользователь -> обычный peer, min -> `InputPeerUserFromMessage`; отказы; файл
и альбом; `reconcile`; имя и id автора в журнал не попадают. Telegram — `FakeGateway` в памяти, сети нет."""

from __future__ import annotations

import logging

import pytest
from telethon import errors, types

from hub.connectors.telegram.connector import TelegramConnector

from ..outbox_file_fakes import PNG_BYTES, FakeFileCore, file_meta
from .helpers import ME_ID, FakeGateway, make_channel, make_chat, make_dialog, make_user

NOW_MS = 1_700_000_000_000
USER_ID = 5550001
GROUP_ID = 4242
GROUP_EXT = f"-100{GROUP_ID:010d}"  # помеченный id супергруппы
GROUP_MARKED = int(GROUP_EXT)
MSG_ID = 812
NAME = "Пенопласт"
MSG_GONE = "сообщение группы недоступно (удалено или нас нет в группе) — написать автору через него нельзя"
CHAT_GONE = "группы больше нет в аккаунте — написать автору через неё нельзя"
BASIC = "в обычной группе написать автору через сообщение нельзя — нужен @ник или номер"
PRIVACY = "человек принимает личку только от контактов/Premium"


def _group_dialog():
    return make_dialog(make_channel(GROUP_ID, title="Продавцы", megagroup=True), top_message=MSG_ID)


def _setup(tmp_path, *, with_group=True):
    core = FakeFileCore()
    gw = FakeGateway()
    gw.now_ms = NOW_MS
    connector = TelegramConnector(
        core.client(),
        gw,
        account_id=1,
        state_path=tmp_path / "state.json",
        media_tmp_dir=tmp_path / "tmp",
        wall=lambda: NOW_MS / 1000,
        mono=lambda: 1000.0,
    )
    connector._me_id = ME_ID  # noqa: SLF001
    if with_group:
        gw.add_dialog(_group_dialog())
    return connector, gw, core


def _lease(*, chat_ext=GROUP_EXT, msg_ext=str(MSG_ID), user_id=USER_ID, maybe_sent=False, **extra) -> dict:
    lease = {
        "outbox_id": 7,
        "lease_gen": 1,
        "channel_ext_id": "new:" + "a" * 32,
        "text": "Здравствуйте",
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": "tok-author",
        "mode": "send",
        "send_started_ts": NOW_MS,
        "attempts": 1,
        "maybe_sent": maybe_sent,
        "known_own_ext_ids": [],
        "files": None,
        "new_to": {
            "kind": "tg_msg_author",
            "user_id": user_id,
            "name": NAME,
            "chat_ext_id": chat_ext,
            "msg_ext_id": msg_ext,
        },
    }
    lease.update(extra)
    return lease


def _author(*, min=False, **kw) -> types.User:
    user = make_user(USER_ID, first_name="Пено", last_name="Пласт", **kw)
    user.access_hash = 777
    user.min = min
    return user


def _no_person(caplog, result=None) -> None:
    for secret in (str(USER_ID), NAME, "Пено", "Пласт"):
        assert secret not in caplog.text
        if result is not None and result.outcome != "sent":
            assert secret not in (result.error or "")


def _branches(caplog) -> list[str]:
    mark = "автор сообщения группы — "
    return [r.getMessage().split(mark)[1] for r in caplog.records if mark in r.getMessage()]


async def test_cache_hit_sends_without_message_access(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _core = _setup(tmp_path)
    gw.seed_peer(USER_ID, types.InputPeerUser(USER_ID, 99))
    result = await connector.send(_lease())
    assert (result.outcome, result.peer_ext_id, result.peer_title) == ("sent", str(USER_ID), NAME)
    assert gw.user_from_message_calls == [] and gw.iter_dialogs_calls == 0
    assert gw.sent[0]["marked_id"] == USER_ID
    assert _branches(caplog) == ["cache"]
    _no_person(caplog)


async def test_full_user_through_message_gets_plain_peer(tmp_path, caplog, monkeypatch) -> None:
    """П7г: шаг 4 вернул полного пользователя — отправка идёт обычным `InputPeerUser`."""
    caplog.set_level(logging.DEBUG)
    connector, gw, _core = _setup(tmp_path)
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author())
    peers = []
    real_send = gw.send_message

    async def spy(peer, **kw):
        peers.append(peer)
        return await real_send(peer, **kw)

    monkeypatch.setattr(gw, "send_message", spy)
    result = await connector.send(_lease())
    assert (result.outcome, result.peer_ext_id, result.peer_title) == ("sent", str(USER_ID), "Пено Пласт")
    assert gw.user_from_message_calls == [(GROUP_MARKED, MSG_ID, USER_ID)]
    assert peers == [types.InputPeerUser(user_id=USER_ID, access_hash=777)]
    assert _branches(caplog) == ["full"]
    _no_person(caplog)


async def test_min_user_keeps_peer_from_message(tmp_path, caplog, monkeypatch) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _core = _setup(tmp_path)
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author(min=True))
    peers = []
    real_send = gw.send_message

    async def spy(peer, **kw):
        peers.append(peer)
        return await real_send(peer, **kw)

    monkeypatch.setattr(gw, "send_message", spy)
    result = await connector.send(_lease())
    assert (result.outcome, result.peer_ext_id) == ("sent", str(USER_ID))
    assert len(peers) == 1 and isinstance(peers[0], types.InputPeerUserFromMessage)
    assert (peers[0].msg_id, peers[0].user_id) == (MSG_ID, USER_ID)
    assert isinstance(peers[0].peer, types.InputPeerChannel) and peers[0].peer.channel_id == GROUP_ID
    assert _branches(caplog) == ["min"]
    _no_person(caplog)


async def test_type_error_from_session_cache_is_a_miss(tmp_path) -> None:
    """П7а: `get_input_entity` бросил `TypeError` (сервер вернул min-пользователя) — промах, идём через
    сообщение, а не падаем."""
    connector, gw, _core = _setup(tmp_path)
    gw.peer_cache_faults[USER_ID] = TypeError("Cannot cast User to any kind of InputPeer.")
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author(min=True))
    result = await connector.send(_lease())
    assert (result.outcome, result.peer_ext_id) == ("sent", str(USER_ID))
    assert gw.user_from_message_calls == [(GROUP_MARKED, MSG_ID, USER_ID)]


async def test_group_peer_found_by_one_dialogs_pass(tmp_path) -> None:
    connector, gw, _core = _setup(tmp_path)
    gw.forget_peer(GROUP_MARKED)
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author())

    real_iter = gw.iter_dialogs

    async def warming():
        async for d in real_iter():
            gw.seed_peer(GROUP_MARKED, types.InputPeerChannel(GROUP_ID, 0))
            yield d

    gw.iter_dialogs = warming
    result = await connector.send(_lease())
    assert result.outcome == "sent" and gw.iter_dialogs_calls == 1


@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ({"deleted": True}, "аккаунт удалён"),
        ({"bot": True}, "автор — бот"),
        ({"is_self": True}, "это ваше сообщение"),
    ],
)
async def test_author_not_writable(tmp_path, caplog, patch, error) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _core = _setup(tmp_path)
    user = _author()
    for key, value in patch.items():
        setattr(user, key, value)
    gw.add_message_author(GROUP_MARKED, MSG_ID, user)
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", error)
    assert gw.send_calls == 0
    _no_person(caplog, result)


@pytest.mark.parametrize(
    "fault",
    [
        errors.MsgIdInvalidError(request=None),
        errors.MessageIdInvalidError(request=None),
        errors.PeerIdInvalidError(request=None),
        errors.ChannelPrivateError(request=None),
        None,  # пустой ответ
        types.UserEmpty(id=USER_ID),
        make_user(USER_ID + 1, first_name="Чужой"),  # в ответе не тот человек
    ],
)
async def test_message_unavailable(tmp_path, caplog, fault) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _core = _setup(tmp_path)
    gw.user_from_message_faults.append(fault)
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", MSG_GONE)
    assert gw.send_calls == 0
    _no_person(caplog, result)


async def test_message_absent_on_server(tmp_path) -> None:
    connector, gw, _core = _setup(tmp_path)  # автора у сообщения поддельный сервер не знает
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", MSG_GONE)


async def test_group_gone_from_account(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _core = _setup(tmp_path, with_group=False)
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", CHAT_GONE)
    assert gw.iter_dialogs_calls == 1 and gw.user_from_message_calls == []
    assert _branches(caplog) == ["no_chat"]


async def test_basic_group_only_through_cache(tmp_path) -> None:
    """П8: обычная группа (не супергруппа): промах кэша — отказ без запросов, попадание — отправка."""
    connector, gw, _core = _setup(tmp_path, with_group=False)
    gw.add_dialog(make_dialog(make_chat(300), top_message=1))
    miss = await connector.send(_lease(chat_ext="-300"))
    assert (miss.outcome, miss.error) == ("permanent", BASIC)
    assert gw.user_from_message_calls == [] and gw.iter_dialogs_calls == 0 and gw.send_calls == 0
    gw.seed_peer(USER_ID, types.InputPeerUser(USER_ID, 1))
    hit = await connector.send(_lease(chat_ext="-300"))
    assert (hit.outcome, hit.peer_ext_id) == ("sent", str(USER_ID))


@pytest.mark.parametrize(
    "new_to",
    [
        {"user_id": "x", "chat_ext_id": GROUP_EXT, "msg_ext_id": "5"},
        {"user_id": USER_ID, "chat_ext_id": "4242", "msg_ext_id": "5"},
        {"user_id": USER_ID, "chat_ext_id": GROUP_EXT, "msg_ext_id": "0"},
        {"user_id": USER_ID, "chat_ext_id": GROUP_EXT, "msg_ext_id": "abc"},
        {"user_id": USER_ID, "chat_ext_id": GROUP_EXT},
        {"user_id": USER_ID, "chat_ext_id": int(GROUP_EXT), "msg_ext_id": 5},
    ],
)
async def test_bad_ref(tmp_path, new_to) -> None:
    connector, gw, _core = _setup(tmp_path)
    result = await connector.send(_lease(new_to={"kind": "tg_msg_author", **new_to}))
    assert (result.outcome, result.error) == ("permanent", "bad_ref")
    assert gw.get_input_entity_calls == [] and gw.send_calls == 0


async def test_own_id_refused_without_requests(tmp_path) -> None:
    connector, gw, _core = _setup(tmp_path)
    result = await connector.send(_lease(user_id=ME_ID))
    assert (result.outcome, result.error) == ("permanent", "это ваше сообщение")
    assert gw.get_input_entity_calls == []


async def test_flood_wait_and_network_are_temporary(tmp_path) -> None:
    connector, gw, _core = _setup(tmp_path)
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author())
    gw.user_from_message_faults.extend(
        [errors.FloodWaitError(request=None, capture=42), ConnectionError("x")]
    )
    flood = await connector.send(_lease())
    assert (flood.outcome, flood.error, flood.retry_after_ms) == ("temporary", "FloodWait", 42_000)
    net = await connector.send(_lease())
    assert (net.outcome, net.error) == ("temporary", "network")
    assert gw.send_calls == 0
    ok = await connector.send(_lease())
    assert ok.outcome == "sent"


async def test_other_bad_request_is_permanent_with_class_name(tmp_path, caplog) -> None:
    """П7в: прочий `BadRequestError` — не `temporary` с повторами, а `permanent` с именем класса."""
    caplog.set_level(logging.DEBUG)
    connector, gw, _core = _setup(tmp_path)
    gw.user_from_message_faults.append(errors.UserIdInvalidError(request=None))
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", "UserIdInvalidError")
    assert _branches(caplog) == ["UserIdInvalidError"]
    _no_person(caplog, result)


async def test_privacy_refusal_at_send_has_clear_text(tmp_path) -> None:
    connector, gw, _core = _setup(tmp_path)
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author(min=True))
    gw.queue_send_faults(errors.UserPrivacyRestrictedError(request=None))
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", PRIVACY)


async def test_peer_id_invalid_at_send_reads_as_message_unavailable(tmp_path) -> None:
    connector, gw, _core = _setup(tmp_path)
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author(min=True))
    gw.queue_send_faults(errors.PeerIdInvalidError(request=None))
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", MSG_GONE)


async def test_peer_id_invalid_text_only_for_this_kind(tmp_path) -> None:
    connector, gw, _core = _setup(tmp_path)
    gw.seed_peer(USER_ID, types.InputPeerUser(USER_ID, 99))
    gw.queue_send_faults(errors.PeerIdInvalidError(request=None))
    lease = _lease(new_to={"kind": "tg_user", "user_id": USER_ID, "name": NAME})
    result = await connector.send(lease)
    assert (result.outcome, result.error) == ("permanent", "PeerIdInvalidError")


async def test_file_and_album_reach_gateway_with_peer_from_message(tmp_path, monkeypatch) -> None:
    """`InputPeerUserFromMessage` доходит до `send_media` и `send_album` без преобразования."""
    connector, gw, core = _setup(tmp_path)
    datas = [PNG_BYTES, PNG_BYTES + b"-2"]
    core.files[7] = datas
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author(min=True))
    peers = []
    real_media, real_album = gw.send_media, gw.send_album

    async def media(peer, **kw):
        peers.append(("media", peer))
        return await real_media(peer, **kw)

    async def album(peer, **kw):
        peers.append(("album", peer))
        return await real_album(peer, **kw)

    monkeypatch.setattr(gw, "send_media", media)
    monkeypatch.setattr(gw, "send_album", album)
    metas = [file_meta(d, name=f"{i}.png") for i, d in enumerate(datas)]
    one = await connector.send(_lease(files=metas[:1]))
    assert (one.outcome, one.peer_ext_id) == ("sent", str(USER_ID))
    two = await connector.send(_lease(send_token="tok-album", files=metas))
    assert (two.outcome, two.peer_ext_id) == ("sent", str(USER_ID))
    assert [kind for kind, _peer in peers] == ["media", "album"]
    assert all(isinstance(peer, types.InputPeerUserFromMessage) for _kind, peer in peers)
    assert {m["marked_id"] for m in gw.media_calls + gw.album_calls} == {USER_ID}


async def test_reconcile_and_reissue_only_read(tmp_path) -> None:
    """Повторная выдача и `reconcile` зовут резолвер снова: только чтение, второй отправки нет, в
    `state.json` ничего не пишется."""
    connector, gw, _core = _setup(tmp_path)
    gw.add_message_author(GROUP_MARKED, MSG_ID, _author(min=True))
    gw.queue_send_faults("network_after")
    first = await connector.send(_lease())
    assert first.outcome != "sent" and len(gw.sent) == 1
    second = await connector.send(_lease(maybe_sent=True))
    assert (second.outcome, second.peer_ext_id) == ("sent", str(USER_ID))
    found = await connector.reconcile(_lease())
    assert (found.outcome, found.peer_ext_id) == ("sent", str(USER_ID))
    assert len(gw.sent) == 1
    assert len(gw.user_from_message_calls) == 3
    assert not (tmp_path / "state.json").exists()
