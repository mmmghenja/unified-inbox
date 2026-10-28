"""Ф34-3 (`docs/architecture.md`–B5 и «Тесты сверх раздела B», П3а, П7), коннектор
Telegram: первое сообщение по номеру телефона — контакты -> кэш процесса -> лимит и пауза в `state.json` ->
`contacts.ResolvePhone`; ошибки; номер не попадает в журнал и `SendResult.error`; импорт контактов не
вызывается. Telegram — `FakeGateway` в памяти, сети нет. Номера — выдуманные."""

from __future__ import annotations

import json
import logging
import pathlib

import pytest
from telethon import errors

from hub.connectors.telegram.connector import TelegramConnector, phone_daily_of

from ..outbox_file_fakes import FakeFileCore
from .helpers import ME_ID, FakeGateway, make_channel, make_user

NOW_MS = 1_700_000_000_000
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS
PHONE = "+79000000001"
DIGITS = "79000000001"
TAIL = "9000000001"
USER_ID = 555
NOT_FOUND = "номер не найден в Telegram или скрыт настройками приватности"


class _Clock:
    def __init__(self) -> None:
        self.ms = NOW_MS

    def __call__(self) -> float:
        return self.ms / 1000


def _setup(tmp_path, *, gw=None, clock=None, daily=5):
    core = FakeFileCore()
    gw = gw or FakeGateway()
    gw.now_ms = NOW_MS
    clock = clock or _Clock()
    connector = TelegramConnector(
        core.client(),
        gw,
        account_id=1,
        state_path=tmp_path / "state.json",
        wall=clock,
        mono=lambda: 1000.0,
        phone_daily=daily,
    )
    connector._me_id = ME_ID  # noqa: SLF001
    return connector, gw, clock


def _lease(phone: str = PHONE, *, token="tok-phone", maybe_sent=False, **extra) -> dict:
    lease = {
        "outbox_id": 7,
        "lease_gen": 1,
        "channel_ext_id": "new:" + "a" * 32,
        "text": "Здравствуйте",
        "reply_to_ext_id": None,
        "thread_ext_id": None,
        "send_token": token,
        "mode": "send",
        "send_started_ts": NOW_MS,
        "attempts": 1,
        "maybe_sent": maybe_sent,
        "known_own_ext_ids": [],
        "files": None,
        "new_to": {"kind": "tg_phone", "phone": phone},
    }
    lease.update(extra)
    return lease


def _person(user_id=USER_ID, *, phone=None, **kw):
    user = make_user(user_id, first_name="Пётр", last_name="Иванов", **kw)
    user.phone = phone
    return user


def _state(tmp_path) -> dict:
    path = tmp_path / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _no_number(caplog, result=None) -> None:
    assert TAIL not in caplog.text
    if result is not None:
        assert TAIL not in (result.error or "")


async def test_found_by_resolve_sends_and_reports_peer(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _clock = _setup(tmp_path)
    gw.add_phone(DIGITS, _person())
    result = await connector.send(_lease())
    assert (result.outcome, result.peer_ext_id, result.peer_title) == ("sent", "555", "Пётр Иванов")
    assert gw.contacts_calls == [0]  # сначала свои контакты
    assert gw.resolve_phone_calls == [DIGITS]  # в запрос — только цифры, без «+»
    assert gw.resolve_calls == []
    assert gw.sent[0]["marked_id"] == USER_ID
    assert _state(tmp_path)["phone_resolve"]["calls"] == [NOW_MS]
    assert TAIL not in (tmp_path / "state.json").read_text(encoding="utf-8")
    _no_number(caplog, result)


async def test_own_contact_found_without_resolve_and_without_counter(tmp_path) -> None:
    connector, gw, _clock = _setup(tmp_path)
    gw.contacts = [_person(777, phone="70000000000", contact=True), _person(phone=DIGITS, contact=True)]
    result = await connector.send(_lease("+7 (900) 000-00-01"))
    assert (result.outcome, result.peer_ext_id) == ("sent", "555")
    assert gw.contacts_calls == [0] and gw.resolve_phone_calls == []
    assert "phone_resolve" not in _state(tmp_path)  # счётчик лимита не тронут


@pytest.mark.parametrize(
    "fault", [None, errors.PhoneNotOccupiedError(request=None), errors.PhoneNumberInvalidError(request=None)]
)
async def test_not_found_or_hidden_is_permanent(tmp_path, caplog, fault) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _clock = _setup(tmp_path)
    if fault is not None:
        gw.resolve_phone_faults.append(fault)
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", NOT_FOUND)
    assert gw.send_calls == 0 and len(gw.resolve_phone_calls) == 1
    _no_number(caplog, result)


async def test_channel_is_permanent_and_bot_is_a_user(tmp_path) -> None:
    connector, gw, _clock = _setup(tmp_path)
    gw.add_phone(DIGITS, make_channel(900, title="Канал", broadcast=True))
    result = await connector.send(_lease())
    assert result.outcome == "permanent" and "не человек" in result.error
    gw.add_phone("79000000002", _person(556, bot=True))
    assert (await connector.send(_lease("+79000000002", token="tok-2"))).outcome == "sent"


@pytest.mark.parametrize("via", ["resolve", "contacts"])
async def test_own_number_is_permanent(tmp_path, via) -> None:
    connector, gw, _clock = _setup(tmp_path)
    gw.me.phone = DIGITS
    if via == "resolve":
        gw.add_phone(DIGITS, gw.me)
    else:
        gw.contacts = [gw.me]
    result = await connector.send(_lease())
    assert (result.outcome, result.error) == ("permanent", "это ваш номер")
    assert gw.send_calls == 0


async def test_floodwait_pauses_resolve_and_next_number_makes_no_request(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, clock = _setup(tmp_path)
    gw.add_phone(DIGITS, _person())
    gw.add_phone("79000000002", _person(556))
    gw.resolve_phone_faults.append(errors.FloodWaitError(request=None, capture=600))
    result = await connector.send(_lease())
    assert (result.outcome, result.error, result.retry_after_ms) == ("temporary", "FloodWait", 600_000)
    assert result.postponed is True  # ожидание попыток не тратит
    assert _state(tmp_path)["phone_resolve"]["pause_until_ms"] == NOW_MS + 600_000

    clock.ms += 60_000
    other = await connector.send(_lease("+79000000002", token="tok-2"))
    assert other.outcome == "temporary" and other.retry_after_ms == 540_000 and other.postponed is True
    assert "на паузе" in other.error
    assert gw.resolve_phone_calls == [DIGITS] and gw.send_calls == 0  # на паузе — БЕЗ запроса

    clock.ms += 540_000
    assert (await connector.send(_lease())).outcome == "sent"
    assert gw.resolve_phone_calls == [DIGITS, DIGITS]
    _no_number(caplog, other)


async def test_flood_error_without_seconds_is_permanent_with_class_name(tmp_path) -> None:
    connector, gw, _clock = _setup(tmp_path)
    gw.resolve_phone_faults.append(errors.PeerFloodError(request=None))
    result = await connector.send(_lease())
    assert (result.outcome, result.error, result.retry_after_ms) == ("permanent", "PeerFloodError", None)
    assert _state(tmp_path)["phone_resolve"]["pause_until_ms"] == 0


async def test_network_and_unexpected_errors_are_temporary_without_number(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, _clock = _setup(tmp_path)
    gw.resolve_phone_faults.extend(
        [ConnectionError(f"обрыв на {PHONE}"), RuntimeError(f"ResolvePhoneRequest(phone='{DIGITS}')")]
    )
    net = await connector.send(_lease())
    assert (net.outcome, net.error) == ("temporary", "network")
    odd = await connector.send(_lease())
    assert (odd.outcome, odd.error) == ("temporary", "RuntimeError")
    assert gw.send_calls == 0
    assert len(_state(tmp_path)["phone_resolve"]["calls"]) == 2  # оборванный вызов — тоже вызов
    _no_number(caplog, net)
    _no_number(caplog, odd)


async def test_reissued_row_does_not_resolve_twice(tmp_path) -> None:
    """Повторная выдача той же строки (`temporary`, истёкший лиз, `reconcile`) — кэш процесса, без запроса."""
    connector, gw, _clock = _setup(tmp_path)
    gw.add_phone(DIGITS, _person())
    gw.queue_send_faults(errors.FloodWaitError(request=None, capture=5))
    first = await connector.send(_lease())
    assert first.outcome == "flood" and gw.resolve_phone_calls == [DIGITS]
    second = await connector.send(_lease(maybe_sent=True))
    assert second.outcome == "sent" and second.peer_ext_id == "555"
    found = await connector.reconcile(_lease())
    assert (found.outcome, found.peer_ext_id) == ("sent", "555")
    assert gw.resolve_phone_calls == [DIGITS]
    assert len(_state(tmp_path)["phone_resolve"]["calls"]) == 1


async def test_daily_limit_no_request_survives_restart_and_frees_after_day(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    connector, gw, clock = _setup(tmp_path)
    for i in range(5):  # ненайденные номера — тоже настоящие вызовы
        result = await connector.send(_lease(f"+7900000001{i}", token=f"tok-{i}"))
        assert result.outcome == "permanent"
        clock.ms += HOUR_MS
    assert len(gw.resolve_phone_calls) == 5

    gw.add_phone(DIGITS, _person())
    over = await connector.send(_lease())
    assert over.outcome == "temporary" and "лимит" in over.error and over.postponed is True
    assert over.retry_after_ms == DAY_MS - 5 * HOUR_MS  # до выхода самого старого вызова из окна
    assert len(gw.resolve_phone_calls) == 5 and gw.send_calls == 0  # на превышении — БЕЗ запроса

    # «рестарт»: новый процесс коннектора с тем же state.json — счёт не обнулился
    restarted, gw2, _clock2 = _setup(tmp_path, clock=clock)
    gw2.add_phone(DIGITS, _person())
    again = await restarted.send(_lease())
    assert again.outcome == "temporary" and gw2.resolve_phone_calls == []

    clock.ms += over.retry_after_ms
    assert (await restarted.send(_lease())).outcome == "sent"
    assert gw2.resolve_phone_calls == [DIGITS]
    # окно снова полно (4 старых + новый): следующий номер ждёт выхода второго вызова
    more = await restarted.send(_lease("+79000000002", token="tok-x"))
    assert more.outcome == "temporary" and more.retry_after_ms == HOUR_MS
    _no_number(caplog, over)


async def test_limit_setting_and_zero_turns_resolve_off(tmp_path) -> None:
    for value, expected in ((2, 2), (0, 0), (50, 20), ("3", 5), (True, 5), (None, 5), (-1, 5)):
        assert phone_daily_of({"tg_phone_daily": value}) == expected
    assert phone_daily_of({}) == 5 and phone_daily_of(None) == 5
    connector, gw, _clock = _setup(tmp_path, daily=0)
    gw.add_phone(DIGITS, _person())
    gw.contacts = [_person(556, phone="79000000002", contact=True)]
    off = await connector.send(_lease())
    assert (off.outcome, off.retry_after_ms) == ("permanent", None) and "выключен" in off.error
    assert gw.resolve_phone_calls == []  # при 0 строка не висит сутками — сразу отказ, без запроса
    # свой контакт лимитом не ограничен
    assert (await connector.send(_lease("+79000000002", token="tok-2"))).outcome == "sent"


async def test_state_file_shared_with_session_alert(tmp_path) -> None:
    connector, gw, _clock = _setup(tmp_path)
    await connector._check_session_alert()  # noqa: SLF001 — первый прогон алерта заводит файл
    assert _state(tmp_path)["initialized"] is True
    gw.add_phone(DIGITS, _person())
    assert (await connector.send(_lease())).outcome == "sent"
    state = _state(tmp_path)
    assert state["initialized"] is True and "known_hashes" in state  # ключи алерта целы
    assert state["phone_resolve"]["calls"] == [NOW_MS]
    await connector._check_session_alert()  # noqa: SLF001
    assert _state(tmp_path)["phone_resolve"]["calls"] == [NOW_MS]  # алерт счётчик не стирает


async def test_corrupt_state_means_no_request(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    (tmp_path / "state.json").write_text("{не json", encoding="utf-8")
    connector, gw, _clock = _setup(tmp_path)
    gw.add_phone(DIGITS, _person())
    result = await connector.send(_lease())
    assert result.outcome == "temporary" and result.retry_after_ms == DAY_MS
    assert gw.resolve_phone_calls == []
    _no_number(caplog, result)


async def test_corrupt_state_healed_by_session_alert_keeps_resolve_paused(tmp_path) -> None:
    """Алерт о сессиях на старте перезаписывает битый файл — лимит номеров при этом не обнуляется."""
    (tmp_path / "state.json").write_text("{не json", encoding="utf-8")
    connector, gw, clock = _setup(tmp_path)
    await connector._check_session_alert()  # noqa: SLF001
    assert _state(tmp_path)["phone_resolve"]["pause_until_ms"] == NOW_MS + DAY_MS
    gw.add_phone(DIGITS, _person())
    restarted, gw2, _clock = _setup(tmp_path, clock=clock)
    gw2.add_phone(DIGITS, _person())
    for conn, gateway in ((connector, gw), (restarted, gw2)):
        result = await conn.send(_lease())
        assert result.outcome == "temporary" and gateway.resolve_phone_calls == []
    clock.ms += DAY_MS
    assert (await restarted.send(_lease())).outcome == "sent"


async def test_lease_without_number_is_permanent(tmp_path) -> None:
    connector, gw, _clock = _setup(tmp_path)
    for new_to in (
        {"kind": "tg_phone"},
        {"kind": "tg_phone", "phone": "+7"},
        {"kind": "tg_phone", "phone": 5},
    ):
        result = await connector.send(_lease(new_to=new_to))
        assert (result.outcome, result.error) == ("permanent", "новая беседа без номера телефона")
    assert gw.contacts_calls == [] and gw.resolve_phone_calls == []


def test_import_contacts_is_absent_from_source() -> None:
    """B5: импорт контактов (добавляет контакт в адресную книгу, за него банят) хаб не вызывает вовсе:
    в коде `src/hub` нет ни запроса, ни метода шлюза."""
    src = pathlib.Path(__file__).resolve().parents[3] / "src" / "hub"
    hits = [str(p) for p in src.rglob("*.py") if "ImportContacts" in p.read_text(encoding="utf-8")]
    assert hits == []
    assert not hasattr(TelegramConnector, "import_contacts")


async def test_fake_gateway_fails_on_import_contacts() -> None:
    """Поддельный шлюз падает при вызове импорта — любой путь, который до него дойдёт, уронит свой тест."""
    with pytest.raises(AssertionError):
        await FakeGateway().import_contacts([])
