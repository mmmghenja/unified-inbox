"""Тест 13 куска 1b: `hub auth tg` с подменённой фабрикой клиента — файлы 0600/каталог 0700, секреты не
попадают в stdout/stderr, повтор без `--account` переиспользует уже существующий аккаунт `telegram` (не
плодит второй) и уважает отказ пользователя заменить креды. Реальный core-стенд в отдельном процессе (та же
`core_env`, что `test_lifecycle.py`) — секрет коннектора пишется через настоящий сокет, как в проде; сеть в
Telegram полностью подменена (`client_factory`).
"""

from types import SimpleNamespace

from telethon.tl.functions.users import GetUsersRequest

from hub.connectors.telegram.auth import cmd_auth_tg
from hub.connectors.telegram.gateway import TgGateway

from .conftest import sync_admin_client

_FAKE_ME = SimpleNamespace(id=1, first_name="владелец", last_name=None, username=None)


class _FakeTelethonClient:
    """Двойник `TelegramClient` — конструктор принимает те же аргументы (`session`, `api_id`, `api_hash`,
    keyword-only опции), но НИЧЕГО не отправляет в сеть."""

    def __init__(self, session, api_id, api_hash, **kwargs):
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.kwargs = kwargs

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def start(self, *, phone=None, code_callback=None, password=None, **_kw):
        # Как настоящий Telethon: значения-функции вызываются, чтобы получить ввод (тест 13 проверяет, что
        # эти значения нигде не печатаются).
        for cb in (phone, code_callback, password):
            if callable(cb):
                cb()
        return self

    async def get_me(self):
        return _FAKE_ME

    async def __call__(self, request):
        # `TgGateway.get_me()` теперь зовёт СЫРОЙ `GetUsersRequest` (находка F1, round 2), а не
        # `client.get_me()` — тот же двойник должен уметь ответить на этот конкретный запрос.
        if isinstance(request, GetUsersRequest):
            return [_FAKE_ME]
        raise AssertionError(f"неожиданный запрос в тесте: {request!r}")


def _args(*, account=None, title=None, proxy=None, new=False):
    return SimpleNamespace(account=account, title=title, proxy=proxy, new=new)


def _run_auth(admin_client, config_dir, data_dir, *, account=None, new=False, inputs, secrets, confirm=None):
    input_iter = iter(inputs)
    secret_iter = iter(secrets)
    return cmd_auth_tg(
        _args(account=account, new=new),
        admin_client=admin_client,
        config_dir=config_dir,
        data_dir=data_dir,
        gateway_factory=TgGateway,
        client_factory=_FakeTelethonClient,
        input_fn=lambda prompt="": next(input_iter),
        getpass_fn=lambda prompt="": next(secret_iter),
        confirm_fn=confirm,
    )


def test_1b_13_files_and_dirs_get_correct_permissions_and_no_secrets_printed(core_env, capsys) -> None:
    data_dir, config_dir = core_env
    secret_api_hash = "api-hash-SEKRET-value"
    secret_password = "2fa-SEKRET-password"

    with sync_admin_client(config_dir, data_dir) as admin_client:
        code = _run_auth(
            admin_client,
            config_dir,
            data_dir,
            inputs=["555123", "+70000000001", "12345"],
            secrets=[secret_api_hash, secret_password],
        )
    assert code == 0

    out, err = capsys.readouterr()
    assert secret_api_hash not in out
    assert secret_api_hash not in err
    assert secret_password not in out
    assert secret_password not in err

    account_id = int(out.splitlines()[0].split("=")[1])

    creds_path = config_dir / "connectors" / f"{account_id}.telegram.json"
    assert oct(creds_path.stat().st_mode & 0o777) == "0o600"

    session_dir = data_dir / "telegram" / str(account_id)
    assert oct(session_dir.stat().st_mode & 0o777) == "0o700"
    session_file = session_dir / "tg.session"
    assert session_file.exists()
    assert oct(session_file.stat().st_mode & 0o777) == "0o600"

    key_path = config_dir / "connectors" / f"{account_id}.key"
    assert oct(key_path.stat().st_mode & 0o777) == "0o600"


def test_1b_13_repeat_without_account_reuses_existing_and_respects_decline(core_env, capsys) -> None:
    data_dir, config_dir = core_env

    with sync_admin_client(config_dir, data_dir) as admin_client:
        code1 = _run_auth(
            admin_client,
            config_dir,
            data_dir,
            inputs=["1", "+7", "111"],
            secrets=["hash1", "pw1"],
        )
        assert code1 == 0
        out1 = capsys.readouterr().out
        account_id_1 = int(out1.splitlines()[0].split("=")[1])

        # Повтор БЕЗ --account: аккаунт telegram уже есть -> переиспользуется (не создаётся второй), но
        # креды для него уже существуют -> спрашивает "заменить?" -> пользователь отказывается -> код 1,
        # никакого нового account_id.
        code2 = _run_auth(
            admin_client,
            config_dir,
            data_dir,
            inputs=[],
            secrets=[],
            confirm=lambda _prompt: False,
        )
        assert code2 == 1

        r = admin_client.get("/admin/status")
        accounts = [a for a in r.json()["accounts"] if a["source"] == "telegram"]

    assert len(accounts) == 1
    assert accounts[0]["id"] == account_id_1


def _tg_accounts(admin_client) -> list[dict]:
    r = admin_client.get("/admin/status")
    return sorted((a for a in r.json()["accounts"] if a["source"] == "telegram"), key=lambda a: a["id"])


def _new_id(capsys) -> int:
    return int(capsys.readouterr().out.splitlines()[0].split("=")[1])


def test_new_creates_second_account_with_own_files_and_ambiguity_is_refused(core_env, capsys) -> None:
    """План 11.33: второй Telegram — `hub auth tg --new`; свои ключ, креды и сессия; без `--account` при
    двух аккаунтах команда не угадывает, а просит выбрать; `--account` несуществующего — отказ."""
    data_dir, config_dir = core_env

    with sync_admin_client(config_dir, data_dir) as admin_client:
        assert (
            _run_auth(admin_client, config_dir, data_dir, inputs=["1", "+7", "111"], secrets=["h1", "p1"])
            == 0
        )
        first_id = _new_id(capsys)

        code = _run_auth(
            admin_client, config_dir, data_dir, new=True, inputs=["2", "+7", "222"], secrets=["h2", "p2"]
        )
        assert code == 0
        second_id = _new_id(capsys)
        assert second_id != first_id

        accounts = _tg_accounts(admin_client)
        assert [a["id"] for a in accounts] == [first_id, second_id]
        assert [a["title"] for a in accounts] == ["Telegram", "Telegram 2"]
        for account_id in (first_id, second_id):
            assert (config_dir / "connectors" / f"{account_id}.key").exists()
            assert (config_dir / "connectors" / f"{account_id}.telegram.json").exists()
            assert (data_dir / "telegram" / str(account_id) / "tg.session").exists()

        # Два аккаунта, ни --account, ни --new -> отказ без вопросов и без нового аккаунта.
        assert _run_auth(admin_client, config_dir, data_dir, inputs=[], secrets=[]) == 1
        assert "несколько" in capsys.readouterr().err

        # --account несуществующего id -> отказ, кредов под ним нет.
        missing = second_id + 100
        assert _run_auth(admin_client, config_dir, data_dir, account=missing, inputs=[], secrets=[]) == 1
        assert not (config_dir / "connectors" / f"{missing}.telegram.json").exists()

        # --new вместе с --account -> отказ.
        assert (
            _run_auth(admin_client, config_dir, data_dir, account=first_id, new=True, inputs=[], secrets=[])
            == 1
        )

        # Явный --account второго — повторный вход именно в него.
        code = _run_auth(
            admin_client,
            config_dir,
            data_dir,
            account=second_id,
            inputs=["2", "+7", "333"],
            secrets=["h2b", "p2b"],
            confirm=lambda _prompt: True,
        )
        assert code == 0
        assert len(_tg_accounts(admin_client)) == 2
