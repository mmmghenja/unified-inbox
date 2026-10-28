"""`hub watch` — НЕ через core (план, разделы 1 и 7): отдельный процесс по таймеру systemd, который замечает
в том числе смерть core. Спецификация: docs/architecture.md

`evaluate()` — чистая функция без сети и файлов (тест 6 спеки: таблица случаев на каждую проблему отдельно
от HTTP и `notify-send`). `run_watch()` — тонкий клиент: `GET /api/v1/healthz` без авторизации, затем (core
жив) `GET /admin/watch` по внутреннему сокету; recipient бэкапа читается ПРЯМО с диска
(`backup_recipient.read_recipient` — модуль без `sqlite3`, общее правило CLI) — не через ручку:
`/admin/watch` отдаёт ровно то, что описано в спеке (`now, accounts, dead_letter_total, outbox_unknown,
last_backup_ts`), без лишнего поля про recipient.

Антиспам — `<data_dir>/watch-state.json` (0600): новая проблема -> уведомить сразу; висящая -> не чаще раза в
30 минут; исчезнувшая -> одно «восстановилось». Битый файл состояния = пустое состояние, не падение.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from . import backup_recipient

_HEALTHZ_TIMEOUT_S = 5.0
_ADMIN_TIMEOUT_S = 5.0
STALE_ACCOUNT_MS = 5 * 60 * 1000
SILENT_CONNECTOR_MS = 5 * 60 * 1000  # раздел E спеки куска 1a: пульс коннектора (last_ok_ts) молчит дольше
BACKUP_STALE_MS = 36 * 60 * 60 * 1000
SESSION_ALERT_WINDOW_MS = 24 * 60 * 60 * 1000  # кусок 1c: «показывает проблему 24 ч после последнего алерта»
REMIND_MS = 30 * 60 * 1000
# Поправки после opus-критика кода куска 2b (R4): ретенция work-БД законно стоит, пока источники молчат
# (защита от скачка часов, раздел F.3 п.3/R3) — но если молчат дольше недели, это стоит заметить руками
# (уволенный коннектор, отозванный токен), а не просто «не стирается».
RETENTION_STALLED_MS = 7 * 24 * 60 * 60 * 1000
AGENT_SILENT_MS = 15 * 60 * 1000  # спека 7b, П6
REMOTE_LOGIN_FAILURES_ALERT = 5  # Ф10-7, решение 4: неверных паролей входа с телефона за сутки
# Ф14-9 П1: до конца сертификата https удалённого слушателя меньше 14 дней — продление (таймер
# hub-cert-renew, раз в сутки; certbot продлевает за 30 дней) явно не идёт.
REMOTE_TLS_WARN_MS = 14 * 24 * 60 * 60 * 1000
# Ф8-5 П12: порог свободного места под медиа — тот же, что `core/media.MIN_FREE_BYTES` (сюда не импортируется:
# модуль CLI без `sqlite3`).
DISK_LOW_BYTES = 5 * 1024**3
# Ф14-6 E: старейшая выдаваемая строка очереди расшифровки старше — служба `hub-transcribe` стоит.
TRANSCRIBE_STALLED_MS = 30 * 60 * 1000
# Ф33-3: метки `scripts/hub-offsite-backup.sh` — <data_dir>/offsite-backup.json; копия вне дома старше.
OFFSITE_STATE_NAME = "offsite-backup.json"
OFFSITE_STALE_MS = 6 * 60 * 60 * 1000
AGENT_STATE_PATH = ("agent", "state.json")  # <data_dir>/agent/state.json — hub.agent.files.state_path
STATE_FILE_NAME = "watch-state.json"
# Проблемы, которые watch видит БЕЗ снимка core — только их исчезновение можно утверждать при partial-прогоне.
_SNAPSHOT_LESS_KEYS = frozenset({"core_down", "watch_unauthorized"})
_INTERNAL_BASE_URL = "http://hub-internal/internal/v1"
# Ф51-1 A4: упавшие юниты коннекторов — `systemctl --user list-units … --state=failed`.
_SYSTEMCTL_TIMEOUT_S = 5.0
_CONNECTOR_UNIT_GLOB = "hub-connector@*"
_CONNECTOR_UNIT_RE = re.compile(r"hub-connector@(\d+)\.service\s")
_SITE_KEY = "site_two_primaries"
# Ф54-2 П8: службы хаба, чей выход 78 (отказ доступа) systemd не перезапускает, — тревожим по `failed`.
_SERVICE_UNITS = {"hub-human-bot.service": "бот владельца в Telegram (hub-human-bot) упал — см. журнал службы"}


@dataclass(frozen=True)
class Problem:
    key: str  # стабильный: "account:3:auth_required", "core_down", "dead_letter", "backup_stale", …
    text: str  # короткий — только источник/название аккаунта и вид проблемы, без текстов сообщений


class Notifier(Protocol):
    def __call__(self, title: str, text: str) -> None: ...


class Sender(Protocol):
    def __call__(self, title: str, text: str, urgency: str) -> None: ...


# --- evaluate(): чистая функция ---------------------------------------------------------------------------


def evaluate(snapshot: dict, now_ms: int) -> list[Problem]:
    """`snapshot` — либо `{"core_down": True}` (шаг 1 не прошёл), либо тело `/admin/watch` плюс
    `core_down: False` и `backup_recipient_configured: bool`, которые добавляет `gather_snapshot()`."""
    if snapshot.get("core_down"):
        return [Problem("core_down", "core недоступен")]
    if snapshot.get("unauthorized"):
        # core жив, но сокет отвечает 401/403 (нет/испорчен `admin.key`): это НЕ `core_down`, и молчать про
        # это нельзя — иначе watch слеп ко всему остальному без единого признака.
        return [Problem("watch_unauthorized", "hub watch не может авторизоваться на сокете (admin.key)")]

    problems: list[Problem] = []
    auth_required_ids: set[int] = set()
    for account in snapshot.get("accounts", []):
        account_id = account["id"]
        status = account.get("status")
        label = f"{account.get('source')} {account.get('title') or account_id}"
        if status == "auth_required":
            # Ф51-1 A4: причина отказа, если ядро её отдало (вид отказа — без адреса и текстов сервера).
            reason = account.get("last_error")
            text = f"{label}: требуется вход" + (f" — {reason}" if isinstance(reason, str) and reason else "")
            problems.append(Problem(f"account:{account_id}:auth_required", text))
            auth_required_ids.add(account_id)
        elif status == "limited":
            problems.append(Problem(f"account:{account_id}:limited", f"{label}: ограничен (flood)"))
        elif status in ("no_network", "stale"):
            last_ok_ts = account.get("last_ok_ts")
            if last_ok_ts is None or now_ms - last_ok_ts > STALE_ACCOUNT_MS:
                text = f"{label}: {status} дольше 5 минут"
                problems.append(Problem(f"account:{account_id}:{status}", text))
        elif account.get("has_connector_secret"):
            # У аккаунта есть настоящий коннектор (не голая запись без источника) и статус не
            # auth_required/limited (у тех — свои проблемы выше) — молчание пульса (`account/status`
            # раз в 60 с, раздел D) дольше 5 минут само по себе подозрительно, даже если статус 'ok'.
            # Ни одного пульса ещё не было — коннектор не запускали (аккаунт заведён `hub auth tg` до входа
            # и до включения юнита): это не «замолчал», тревога кричала бы всё время входа (находка 4а
            # критика 1a). Коннектор, который не стартует вовсе, виден в `systemctl --user status`.
            last_ok_ts = account.get("last_ok_ts")
            if last_ok_ts is not None and now_ms - last_ok_ts > SILENT_CONNECTOR_MS:
                problems.append(Problem(f"account:{account_id}:silent", f"{label}: коннектор молчит"))

        # Кусок 1c, раздел «Алерт о новой сессии Telegram»: независимо от статуса выше — новая сессия
        # входа видна 24 ч после последнего алерта, потом сама гаснет (не привязана к auth_required/limited).
        last_alert_ts = account.get("last_alert_ts")
        if last_alert_ts is not None and now_ms - last_alert_ts <= SESSION_ALERT_WINDOW_MS:
            problems.append(Problem(f"account:{account_id}:alert", f"{label}: новая сессия входа"))

    # Ф51-1 A4: юнит коннектора в `failed` (выход 78 systemd не перезапускает). У аккаунта, по которому
    # проблема уже названа выше (`account:N:*` — требуется вход, молчит, …), вторую не добавляем.
    named = {p.key.split(":")[1] for p in problems if p.key.startswith("account:")}
    for unit_account in snapshot.get("failed_connector_units") or []:
        if unit_account in auth_required_ids or str(unit_account) in named:
            continue
        problems.append(
            Problem(
                f"connector_unit:{unit_account}:failed",
                f"коннектор аккаунта {unit_account} упал — см. журнал hub-connector@{unit_account}",
            )
        )

    # Ф69-1 E: стоп первых сообщений новым людям (PeerFlood) — при любом статусе аккаунта; после юнитов, чтобы
    # эта проблема не прятала «коннектор упал» (`named` выше).
    for account in snapshot.get("accounts", []):
        if account.get("peer_flood_ts") is not None:
            label = f"{account.get('source')} {account.get('title') or account['id']}"
            problems.append(
                Problem(f"account:{account['id']}:peer_flood", f"{label}: стоп первых сообщений новым людям")
            )

    for unit in snapshot.get("failed_service_units") or []:
        if unit in _SERVICE_UNITS:
            problems.append(Problem(f"service_unit:{unit}:failed", _SERVICE_UNITS[unit]))

    dead_letter_total = snapshot.get("dead_letter_total") or 0
    if dead_letter_total > 0:
        problems.append(Problem("dead_letter", f"{dead_letter_total} событий в dead_letter"))

    outbox_unknown = snapshot.get("outbox_unknown") or 0
    if outbox_unknown > 0:
        problems.append(Problem("outbox_unknown", f"{outbox_unknown} сообщений со статусом unknown"))

    if snapshot.get("backup_recipient_configured"):
        last_backup_ts = snapshot.get("last_backup_ts")
        if last_backup_ts is None or now_ms - last_backup_ts > BACKUP_STALE_MS:
            problems.append(Problem("backup_stale", "бэкап не выполнялся дольше 36 часов"))

    # Ф33-3: файла меток нет — копия вне дома не заведена (`hub-offsite-backup.sh init` не запускался),
    # молчим, как `backup_stale` без recipient. Есть — отсчёт от последней копии вне дома, а до первой —
    # от первого прогона.
    if snapshot.get("offsite_state_present"):
        ref = snapshot.get("offsite_ok_ts") or snapshot.get("offsite_since_ts")
        if not isinstance(ref, int) or now_ms - ref > OFFSITE_STALE_MS:
            problems.append(Problem("offsite_backup_stale", "копия вне дома старше 6 часов"))

    # Поправки после opus-критика кода (R4): отметки времени источников ещё не было (свежая установка) ->
    # молчать про это тут не о чем — сравнивать не с чем.
    source_time_hwm = snapshot.get("source_time_hwm")
    if source_time_hwm is not None and now_ms - source_time_hwm > RETENTION_STALLED_MS:
        silent_days = (now_ms - source_time_hwm) // (24 * 60 * 60 * 1000)
        # Ф8-4 П9: ретенция снята — проблема осталась про то, что источники молчат (ключ прежний, чтобы не
        # сбить дедуп уведомлений hub-watch).
        problems.append(Problem("retention_stalled", f"источники молчат {silent_days} дней"))

    # Ф8-5 П12: свободно меньше 5 ГиБ — «медиа: X ГБ, свободно Y ГБ» (новые медиа уже ложатся `deferred`).
    media = snapshot.get("media")
    if isinstance(media, dict):
        free = media.get("free_bytes")
        if isinstance(free, int) and free < DISK_LOW_BYTES:
            total = media.get("total_bytes") if isinstance(media.get("total_bytes"), int) else 0
            gib = 1024**3
            problems.append(Problem("disk_low", f"медиа: {total / gib:.1f} ГБ, свободно {free / gib:.1f} ГБ"))

    # Ф10-7, решение 4: подбор пароля входа с телефона — уведомление на ноутбуке.
    remote_failures = snapshot.get("remote_login_failures_24h")
    if isinstance(remote_failures, int) and remote_failures >= REMOTE_LOGIN_FAILURES_ALERT:
        problems.append(
            Problem("remote_login", f"{remote_failures} неверных паролей входа с телефона за сутки")
        )

    # Ф14-9 П1: пара https удалённого слушателя не грузится (вход с телефона не поднят) или кончается.
    remote_tls = snapshot.get("remote_tls")
    if isinstance(remote_tls, dict):
        tls_error = remote_tls.get("error")
        not_after_ms = remote_tls.get("not_after_ms")
        if tls_error:
            problems.append(
                Problem("remote_tls", f"https для телефона: ОШИБКА — {tls_error}, вход с телефона не поднят")
            )
        elif isinstance(not_after_ms, int) and not_after_ms - now_ms < REMOTE_TLS_WARN_MS:
            days = (not_after_ms - now_ms) // (24 * 60 * 60 * 1000)
            left = f"осталось {days} {_days_word(days)}" if not_after_ms > now_ms else "истёк"
            problems.append(
                Problem(
                    "remote_tls",
                    f"сертификат https для телефона: {left} — продление не идёт? "
                    "journalctl --user -u hub-cert-renew",
                )
            )

    # Ф14-6 E: служба расшифровки заведена (живой токен `transcribe:worker`), а очередь стоит дольше 30 минут.
    transcribe = snapshot.get("transcribe")
    if isinstance(transcribe, dict) and transcribe.get("configured"):
        oldest = transcribe.get("oldest_queued_ts")
        if isinstance(oldest, int) and now_ms - oldest > TRANSCRIBE_STALLED_MS:
            minutes = (now_ms - oldest) // 60_000
            problems.append(
                Problem("transcribe_stalled", f"расшифровка голосовых стоит: очередь ждёт {minutes} мин")
            )

    # Спека 7b, П6: дежурный агент настроен (его файл состояния существует), а успешного цикла опроса не было
    # дольше 15 минут — «дежурный молчит». Нет файла — агент не включён, молчать не о чем.
    if snapshot.get("agent_state_present"):
        agent_last_ok_ts = snapshot.get("agent_last_ok_ts")
        if not isinstance(agent_last_ok_ts, int) or now_ms - agent_last_ok_ts > AGENT_SILENT_MS:
            problems.append(Problem("agent_silent", "дежурный агент молчит дольше 15 минут"))

    return problems


def _days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


# --- антиспам -----------------------------------------------------------------------------------------


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
    finally:
        os.chmod(path, 0o600)


def notify_send(
    title: str,
    text: str,
    *,
    notifier: Notifier | None = None,
    urgency: str = "critical",
    quiet: bool = False,
) -> None:
    """`notify-send --app-name=hub -u critical` (для «восстановилось» — `normal`: critical в GNOME не гаснет
    сам, а хорошая новость не должна копиться несгораемыми плашками). Нет бинарника или нет D-Bus-сессии
    (linger без графики) -> строка в stderr, БЕЗ исключения: `hub watch` не должен краснеть в systemd из-за
    отсутствия экрана. `notifier` — инъекция для тестов (настоящий `notify-send` там не зовём).

    `quiet=True` (спека 7b, П8: дежурный агент) — в stderr при отсутствии/ошибке `notify-send` НЕ печатаются
    ни заголовок, ни текст (в них название беседы и пересказ чужих сообщений) — только сам факт неудачи."""
    if notifier is not None:
        notifier(title, text)
        return
    shown = "(заголовок и текст скрыты)" if quiet else f"{title}: {text}"
    binary = shutil.which("notify-send")
    if binary is None:
        print(f"hub: notify-send не найден -- {shown}", file=sys.stderr)
        return
    try:
        subprocess.run(  # noqa: S603 — без shell, путь из shutil.which, аргументы не от внешнего ввода
            # `--`: заголовок аккаунта приходит из БД и может начинаться с дефиса — не давать GLib
            # разобрать его как опцию.
            [binary, "--app-name=hub", "-u", urgency, "--", title, text],
            check=True,
            timeout=5,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"hub: notify-send упал ({exc.__class__.__name__}) -- {shown}", file=sys.stderr)


def notify_via_core(
    title: str,
    text: str,
    *,
    urgency: str = "critical",
    quiet: bool = False,
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    admin_secret: str | None = None,
    report: bool = True,
) -> bool:
    """Ф33-2: см. `_post_core`; `True` — ядро приняло уведомление (слушает ли его спутник — не важно)."""
    return (
        _post_core(
            title,
            text,
            urgency=urgency,
            quiet=quiet,
            config_dir=config_dir,
            data_dir=data_dir,
            admin_secret=admin_secret,
            report=report,
        )
        is not None
    )


def notify_core_or_direct(
    title: str,
    text: str,
    *,
    urgency: str = "critical",
    quiet: bool = False,
    config_dir: Path | None = None,
    data_dir: Path | None = None,
    admin_secret: str | None = None,
) -> None:
    """Ф33-2: уведомление в кольцо ядра; ядро не приняло ИЛИ за последние 3 минуты никто не слушал кольцо
    (`listened: false` — спутник `hub notify` не включён) — ещё и прямой `notify-send`, как раньше. Спутник
    слушает — прямого вызова нет, дублей нет."""
    reply = _post_core(
        title,
        text,
        urgency=urgency,
        quiet=quiet,
        config_dir=config_dir,
        data_dir=data_dir,
        admin_secret=admin_secret,
        report=False,
    )
    if reply is None or not reply.get("listened"):
        notify_send(title, text, urgency=urgency, quiet=quiet)


def _post_core(
    title: str,
    text: str,
    *,
    urgency: str,
    quiet: bool,
    config_dir: Path | None,
    data_dir: Path | None,
    admin_secret: str | None,
    report: bool,
) -> dict | None:
    """Ф33-2: уведомление владельца кладётся в кольцо ядра (`POST /internal/v1/notify` по сокету, секрет админа),
    дальше его показывает `hub notify` на ноутбуке. Ядро недоступно -> `False` и (при `report`) строка в
    stderr (в `quiet` — без заголовка и текста), БЕЗ исключения: службы не краснеют из-за уведомления."""
    from . import paths  # лениво: paths создаёт каталоги, тестам и `evaluate` это не нужно

    cfg = config_dir if config_dir is not None else paths.config_dir()
    data = data_dir if data_dir is not None else paths.data_dir()
    if admin_secret is None:
        key_path = cfg / "admin.key"
        admin_secret = key_path.read_text(encoding="utf-8").strip() if key_path.exists() else ""
    shown = "(заголовок и текст скрыты)" if quiet else f"{title}: {text}"
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(data / "internal.sock")),
            base_url=_INTERNAL_BASE_URL,
            headers={"Authorization": f"Bearer {admin_secret}"},
            timeout=_ADMIN_TIMEOUT_S,
        ) as client:
            r = client.post("/notify", json={"title": title, "text": text, "urgency": urgency})
        ok = r.status_code == 200
        reason = f"HTTP {r.status_code}"
        reply = r.json() if ok else None
        if reply is not None and not isinstance(reply, dict):
            reply = {}
    except (httpx.HTTPError, ValueError) as exc:
        reply, reason = None, exc.__class__.__name__
    if reply is None and report:
        print(f"hub: ядро не приняло уведомление ({reason}) -- {shown}", file=sys.stderr)
    return reply


def apply_antispam(
    problems: list[Problem],
    state: dict,
    now_ms: int,
    *,
    notifier: Notifier | None = None,
    partial: bool = False,
    unknown_prefixes: tuple[str, ...] = (),
    sender: Sender | None = None,
) -> dict:
    """Не мутирует `state` — возвращает НОВОЕ состояние. Новая проблема (её `key` не было в `state`) ->
    уведомляем сразу; уже известная -> не чаще раза в `REMIND_MS`; пропавшая (была в `state`, нет среди
    `problems`) -> одно уведомление «восстановилось» и убирается из состояния.

    `partial=True` — снимок core получить не удалось (`core_down`/`watch_unauthorized`): о прочих проблемах
    ничего не известно, поэтому они НЕ объявляются восстановившимися, а переносятся в новое состояние как
    есть. Иначе падение core рассылало бы «dead_letter восстановилось», а его возврат — повторный залп.

    `unknown_prefixes` (Ф51-1 A4) — виды проблем, о которых этот прогон ничего не узнал (не ответил
    `systemctl`): их прежние записи переносятся как есть — ни тревоги, ни «восстановилось».

    `sender(title, text, urgency)` (Ф33-2) — куда уходят уведомления без инъекции `notifier`; по умолчанию
    прямой `notify-send`."""
    send = sender or (lambda title, text, urgency: notify_send(title, text, urgency=urgency))

    def emit(title: str, text: str, urgency: str = "critical") -> None:
        if notifier is not None:
            notifier(title, text)
        else:
            send(title, text, urgency)

    prior: dict[str, dict] = dict(state.get("problems", {}))
    next_problems: dict[str, dict] = {}
    current_keys = {p.key for p in problems}

    for problem in problems:
        entry = prior.get(problem.key)
        if entry is None:
            emit(f"hub: {problem.key}", problem.text)
            next_problems[problem.key] = {"first_ts": now_ms, "last_notified_ts": now_ms}
            continue
        last_notified_ts = entry.get("last_notified_ts", entry.get("first_ts", now_ms))
        if now_ms - last_notified_ts >= REMIND_MS:
            emit(f"hub: {problem.key}", problem.text)
            next_problems[problem.key] = {
                "first_ts": entry.get("first_ts", now_ms),
                "last_notified_ts": now_ms,
            }
        else:
            next_problems[problem.key] = entry

    for key, entry in prior.items():
        if key in current_keys:
            continue
        if (partial and key not in _SNAPSHOT_LESS_KEYS) or key.startswith(unknown_prefixes or "\0"):
            next_problems[key] = entry
        else:
            emit(f"hub: {key}", "восстановилось", "normal")

    return {"problems": next_problems}


# --- сбор снимка и запуск -------------------------------------------------------------------------------


def gather_snapshot(
    *, port: int, data_dir: Path, config_dir: Path, admin_secret: str, socket_path: Path | None = None
) -> dict:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/v1/healthz", timeout=_HEALTHZ_TIMEOUT_S)
        core_alive = r.status_code == 200
    except httpx.HTTPError:
        core_alive = False

    if not core_alive:
        return {"core_down": True}

    sock = socket_path if socket_path is not None else data_dir / "internal.sock"
    transport = httpx.HTTPTransport(uds=str(sock))
    try:
        with httpx.Client(
            transport=transport,
            base_url=_INTERNAL_BASE_URL,
            headers={"Authorization": f"Bearer {admin_secret}"},
            timeout=_ADMIN_TIMEOUT_S,
        ) as client:
            r = client.get("/admin/watch")
            if r.status_code in (401, 403):
                return {"unauthorized": True}
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError, ValueError:
        # healthz ответил, но сокет между тем лёг (гонка) — тоже считаем core мёртвым, не роняем watch.
        return {"core_down": True}

    return {
        **body,
        "core_down": False,
        "backup_recipient_configured": backup_recipient.read_recipient(config_dir) is not None,
        **_agent_snapshot(data_dir),
        **_offsite_snapshot(data_dir),
        "failed_connector_units": failed_connector_units(),
        "failed_service_units": failed_service_units(),
    }


def failed_connector_units() -> list[int] | None:
    """Ф51-1 A4: номера аккаунтов, чей юнит `hub-connector@N` стоит в `failed`. Сбой команды (нет systemd,
    нет шины пользователя, таймаут) — `None`, «неизвестно»: не ошибка сторожа и не «упавших нет»."""
    output = _list_failed([_CONNECTOR_UNIT_GLOB])
    return None if output is None else parse_failed_units(output)


def failed_service_units() -> list[str] | None:
    """Ф54-2 П8: какие из служб `_SERVICE_UNITS` стоят в `failed`; сбой команды — `None`, «неизвестно»."""
    output = _list_failed(sorted(_SERVICE_UNITS))
    return None if output is None else parse_failed_services(output)


def parse_failed_services(output: str) -> list[str]:
    names = {line.split()[0] for line in output.splitlines() if line.split()}
    return sorted(names & set(_SERVICE_UNITS))


def _list_failed(patterns: list[str]) -> str | None:
    binary = shutil.which("systemctl")
    if binary is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 — без shell, путь из shutil.which, аргументы постоянные
            [binary, "--user", "list-units", *patterns, "--state=failed", "--no-legend", "--plain"],
            check=True,
            timeout=_SYSTEMCTL_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout


def parse_failed_units(output: str) -> list[int]:
    """Строки `hub-connector@6.service loaded failed failed …` -> `[6]` (по возрастанию, без повторов)."""
    found: set[int] = set()
    for line in output.splitlines():
        match = _CONNECTOR_UNIT_RE.match(line.strip())
        if match:
            found.add(int(match.group(1)))
    return sorted(found)


def _agent_snapshot(data_dir: Path) -> dict:
    """Спека 7b, П6: есть ли файл состояния дежурного агента и его `last_ok_ts`. Битый файл — есть, но без
    отметки (значит, «молчит»)."""
    path = data_dir.joinpath(*AGENT_STATE_PATH)
    if not path.exists():
        return {"agent_state_present": False}
    state = _load_state(path)
    last_ok_ts = state.get("last_ok_ts") if isinstance(state, dict) else None
    return {"agent_state_present": True, "agent_last_ok_ts": last_ok_ts}


def _offsite_snapshot(data_dir: Path) -> dict:
    """Ф33-3: метки копии вне дома (`scripts/hub-offsite-backup.sh`). Битый файл — есть, но без меток
    (значит, тревога)."""
    path = data_dir / OFFSITE_STATE_NAME
    if not path.exists():
        return {"offsite_state_present": False}
    state = _load_state(path)
    if not isinstance(state, dict):
        state = {}
    if "offsite_repo" in state and not state["offsite_repo"]:
        # Копия вне дома выключена настройкой (пустой HUB_OFFSITE_REPO) — тревожить не о чем.
        return {"offsite_state_present": False}
    return {
        "offsite_state_present": True,
        "offsite_ok_ts": state.get("offsite_ok_ts"),
        "offsite_since_ts": state.get("since_ts"),
    }


def _watch_sender(snapshot: dict, *, config_dir: Path, data_dir: Path, admin_secret: str) -> Sender:
    """Ф33-2: живое ядро — уведомление в кольцо ядра (не принялось — прямой `notify-send`); мёртвое ядро —
    прямой `notify-send` (если экран есть) и одна строка в Telegram ботом хаба. Тревога и «восстановилось» —
    по одному сообщению: сюда приходит ровно одно уведомление на каждое."""
    core_dead = bool(snapshot.get("core_down"))

    def send(title: str, text: str, urgency: str) -> None:
        if not core_dead:
            notify_core_or_direct(
                title,
                text,
                urgency=urgency,
                config_dir=config_dir,
                data_dir=data_dir,
                admin_secret=admin_secret,
            )
            return
        notify_send(title, text, urgency=urgency)
    return send


def run_watch(
    *,
    port: int,
    data_dir: Path,
    config_dir: Path,
    admin_secret: str,
    now_ms: int,
    notifier: Notifier | None = None,
    site_runner=None,
) -> int:
    """Один шаг таймера. Всегда возвращает 0 (спека: `hub watch` не должен краснеть в systemd ни из-за
    мёртвого core, ни из-за отсутствия `notify-send`/сессии D-Bus)."""
    snapshot = gather_snapshot(port=port, data_dir=data_dir, config_dir=config_dir, admin_secret=admin_secret)
    state_path = data_dir / STATE_FILE_NAME
    state = _load_state(state_path)
    partial = bool(snapshot.get("core_down") or snapshot.get("unauthorized"))
    site_entry = None
    if site_entry is not None:
        snapshot["site_witness"] = site_entry
    problems = evaluate(snapshot, now_ms)
    site_unknown = site_entry is not None and bool(site_entry.get("unknown"))
    # Ф51-1 A4: `systemctl` не ответил — про упавшие юниты на этом прогоне ничего не известно.
    units_unknown = not partial and snapshot.get("failed_connector_units") is None
    services_unknown = not partial and snapshot.get("failed_service_units") is None
    new_state = apply_antispam(
        problems,
        state,
        now_ms,
        notifier=notifier,
        sender=_watch_sender(snapshot, config_dir=config_dir, data_dir=data_dir, admin_secret=admin_secret),
        partial=partial,
        unknown_prefixes=(("connector_unit:",) if units_unknown else ())
        + (("service_unit:",) if services_unknown else ())
        + ((_SITE_KEY,) if site_unknown else ()),
    )
    if site_entry is not None:
        new_state["site"] = site_entry
    try:
        _save_state(state_path, new_state)
    except OSError as exc:
        # Без сохранённого состояния следующий прогон повторит уведомление — шумно, но не слепо.
        print(f"hub watch: состояние не сохранено ({exc.__class__.__name__})", file=sys.stderr)
    return 0
