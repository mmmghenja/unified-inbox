"""Старт коннектора ждёт ядро внутри процесса (`cli._connector_wait_account`)."""

import asyncio
import os
import signal

import httpx

from hub import cli


class _Client:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def get_account(self):
        self.calls += 1
        out = self.outcomes.pop(0) if self.outcomes else {"source": "fake"}
        if isinstance(out, Exception):
            raise out
        return out


def _status(code):
    req = httpx.Request("GET", "http://x/account")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


async def test_retries_on_connect_error_and_5xx_then_ok(capsys):
    client = _Client([httpx.ConnectError("no"), _status(503), httpx.ReadTimeout("t"), {"source": "fake"}])
    got = await cli._connector_wait_account(client, 7, steps=(0.01,), log_every_s=3600)
    assert got == {"source": "fake"} and client.calls == 4
    # первая неудача залогирована, дальше — не чаще раза в log_every_s
    assert capsys.readouterr().err.count("ядро не отвечает") == 1


async def test_401_gives_78_without_retry():
    client = _Client([_status(401)])
    assert await cli._connector_wait_account(client, 7, steps=(0.01,)) == 78
    assert client.calls == 1


async def test_sigterm_during_wait_exits_at_once():
    client = _Client([httpx.ConnectError("no")] * 1000)
    task = asyncio.create_task(cli._connector_wait_account(client, 7, steps=(30.0,)))
    await asyncio.sleep(0.1)
    os.kill(os.getpid(), signal.SIGTERM)
    assert await asyncio.wait_for(task, 2) is None
