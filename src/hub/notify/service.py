"""`hub notify run` — long-poll кольца уведомлений ядра и показ через `notify-send` (Ф33-2).

Первый запрос без `after` ставит «с этого момента» (старые уведомления не всплывают). Номер меньше своего
или другая метка запуска ядра (`epoch`) — ядро перезапускалось: ядро само отдаёт кольцо с начала и `reset`,
а `after` берётся из его `last`. Ядро недоступно дольше `DOWN_AFTER_S` — одно местное «хаб недоступен»,
вернулось — «хаб снова на связи»."""

import logging
import signal
import threading
import time
from collections.abc import Callable

import httpx

from . import files

_logger = logging.getLogger("hub.notify")

EXIT_UNAUTHORIZED = 78
DOWN_AFTER_S = 120.0
RETRY_S = 5.0
WAIT_S = 50
HTTP_TIMEOUT_S = WAIT_S + 15.0

Show = Callable[[str, str, str], None]


def _default_show(title: str, text: str, urgency: str) -> None:
    from hub import watch

    watch.notify_send(title, text, urgency=urgency)


class Companion:
    def __init__(
        self,
        *,
        client: httpx.Client,
        token: str,
        show: Show = _default_show,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}
        self._show = show
        self._clock = clock
        self._sleep = sleep
        self.after: int | None = None
        self.epoch: str | None = None
        self._down_since: float | None = None
        self._down_shown = False

    def _failed(self) -> None:
        now = self._clock()
        if self._down_since is None:
            self._down_since = now
        if not self._down_shown and now - self._down_since >= DOWN_AFTER_S:
            self._down_shown = True
            self._show("hub: хаб недоступен", "ядро не отвечает больше 2 минут", "critical")
        self._sleep(RETRY_S)

    def step(self, timeout_s: int = WAIT_S) -> int | None:
        """Один long-poll. `None` — продолжать, число — код выхода (токен отозван)."""
        params: dict = {"timeout_s": timeout_s}
        if self.after is not None:
            params["after"] = self.after
        if self.epoch is not None:
            params["epoch"] = self.epoch
        try:
            r = self._client.get("/notify/wait", params=params, headers=self._headers)
        except httpx.HTTPError:
            self._failed()
            return None
        if r.status_code in (401, 403):
            _logger.error("notify: ядро не принимает токен — hub notify setup")
            return EXIT_UNAUTHORIZED
        if r.status_code != 200:
            self._failed()
            return None
        try:
            body = r.json()
            last = int(body["last"])
            items = list(body["items"])
        except ValueError, KeyError, TypeError:
            self._failed()
            return None
        if self._down_shown:
            self._show("hub: хаб снова на связи", "ядро отвечает", "normal")
        self._down_since, self._down_shown = None, False
        self.epoch = body.get("epoch", self.epoch)
        for item in items:
            self._show(
                str(item.get("title", "")), str(item.get("text", "")), str(item.get("urgency", "critical"))
            )
        self.after = last
        return None


def run(*, config_dir, base_url: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    token = files.read_token(config_dir)
    if token is None:
        _logger.error("notify: нет токена — hub notify setup")
        return EXIT_UNAUTHORIZED
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    _logger.info("notify: старт, ядро %s", base_url)
    with httpx.Client(base_url=base_url, timeout=HTTP_TIMEOUT_S, trust_env=False) as client:
        companion = Companion(client=client, token=token, sleep=lambda s: stop.wait(s))
        while not stop.is_set():
            code = companion.step()
            if code is not None:
                return code
    return 0
