"""Поток `GET /term/stream` (К2, К7, К10, К13): ОДИН поток на вкладку браузера, кадры всех терминалов с
`term_id`.

Позиция терминала — `<эпоха моста>:<n>`; вектор позиций всех терминалов потока — в `id:` каждого кадра с
данными (`3@a1b2c3d4:1024,5@…`), браузер при переподключении присылает его `Last-Event-ID` (важнее параметра
`pos`). Чужая эпоха, позиция вне кольца, новый для потока терминал — кадр `snapshot`, дальше — `out`.

Кадры:
- `snapshot` `{term_id, pos, cols, rows, data_b64, cursor:[x,y], alt_screen, modes}`;
- `out` `{term_id, pos, data_b64}` — склейка 30 мс, данные кадра ≤ 64 КиБ, `pos` — позиция ПОСЛЕ данных;
- `size` `{term_id, cols, rows}` — размер окна сервера поменялся (зрители делают `resize` без `fit`, К10);
- `closed` `{term_id}` — терминала больше нет.
Тишина — комментарий-пульс `: hb` раз в 15 с."""

import asyncio
import base64
import json
import re
from collections.abc import AsyncIterator, Callable

from .bridge import TermGone
from .hub import TermHub
from .tmux import TmuxError

HEARTBEAT_S = 15.0
COALESCE_S = 0.03
FRAME_MAX = 64 * 1024
HEARTBEAT_FRAME = ": hb\n\n"
POS_HEADER_MAX = 1024
_ITEM_RE = re.compile(r"^([1-9][0-9]{0,8})@([0-9a-f]{8}):([0-9]{1,18})$")
_MAX_ITEMS = 32


class BadPos(ValueError):
    pass


def parse_positions(raw: str | None) -> dict[int, tuple[str, int]]:
    """`3@a1b2c3d4:1024,5@…` -> `{3: ("a1b2c3d4", 1024)}`; пусто — `{}`; мусор — `BadPos`."""
    if raw is None or raw == "":
        return {}
    if len(raw) > POS_HEADER_MAX:
        raise BadPos("длинно")
    result: dict[int, tuple[str, int]] = {}
    items = raw.split(",")
    if len(items) > _MAX_ITEMS:
        raise BadPos("много")
    for item in items:
        match = _ITEM_RE.match(item)
        if match is None:
            raise BadPos("формат")
        result[int(match.group(1))] = (match.group(2), int(match.group(3)))
    return result


def format_positions(positions: dict[int, tuple[str, int]]) -> str:
    return ",".join(f"{tid}@{epoch}:{n}" for tid, (epoch, n) in sorted(positions.items()))


def _frame(event: str, data: dict, positions: dict[int, tuple[str, int]] | None) -> str:
    head = f"id: {format_positions(positions)}\n" if positions is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def stream(
    hub: TermHub,
    *,
    positions: dict[int, tuple[str, int]],
    heartbeat_s: float = HEARTBEAT_S,
    still_valid: Callable[[], bool] | None = None,
) -> AsyncIterator[str]:
    clock = asyncio.get_running_loop().time
    sub = hub.subscribe()
    if sub is None:
        return
    state = dict(positions)
    known: set[int] = set(state)  # терминалы, о которых браузер знает (для кадра `closed`)
    sizes: dict[int, tuple[int, int]] = {}
    try:
        if hub.closed or (still_valid is not None and not still_valid()):
            return
        await hub.list()
        next_hb = clock() + heartbeat_s
        next_check = clock() + heartbeat_s
        while True:
            if hub.closed:
                return
            sub.event.clear()
            frames: list[str] = []
            live = hub.live_ids()
            for tid in sorted(known - live):
                known.discard(tid)
                state.pop(tid, None)
                sizes.pop(tid, None)
                frames.append(_frame("closed", {"term_id": tid}, state))
            for tid in sorted(live):
                frames.extend(await _sync_term(hub, tid, state, known, sizes))
                if hub.closed:
                    return
            for frame in frames:
                yield frame
            if frames:
                next_hb = clock() + heartbeat_s
            woke = False
            while not woke:
                timeout = max(0.0, min(next_hb, next_check) - clock())
                try:
                    async with asyncio.timeout(timeout):
                        await sub.event.wait()
                    woke = True
                except TimeoutError:
                    pass
                if hub.closed:
                    return
                if clock() >= next_check:
                    if still_valid is not None and not still_valid():
                        return
                    next_check = clock() + heartbeat_s
                if not woke and clock() >= next_hb:
                    yield HEARTBEAT_FRAME
                    next_hb = clock() + heartbeat_s
            await asyncio.sleep(COALESCE_S)
    finally:
        hub.unsubscribe(sub)


async def _sync_term(
    hub: TermHub,
    tid: int,
    state: dict[int, tuple[str, int]],
    known: set[int],
    sizes: dict[int, tuple[int, int]],
) -> list[str]:
    frames: list[str] = []
    try:
        bridge = await hub.bridge(tid)
        pos = state.get(tid)
        if pos is None or pos[0] != bridge.epoch or not bridge.ring.has(pos[1]):
            snap = await bridge.snapshot()
            state[tid] = (snap["epoch"], snap["pos"])
            known.add(tid)
            sizes[tid] = (snap["cols"], snap["rows"])
            frames.append(
                _frame(
                    "snapshot",
                    {
                        "term_id": tid,
                        "pos": f"{snap['epoch']}:{snap['pos']}",
                        "cols": snap["cols"],
                        "rows": snap["rows"],
                        "data_b64": _b64(snap["data"]),
                        "cursor": snap["cursor"],
                        "alt_screen": snap["alt_screen"],
                        "modes": snap["modes"],
                    },
                    state,
                )
            )
        known.add(tid)
        epoch, n = state[tid]
        while bridge.ring.has(n) and n < bridge.ring.end:
            data = bridge.ring.read(n, FRAME_MAX)
            n += len(data)
            state[tid] = (epoch, n)
            frames.append(
                _frame("out", {"term_id": tid, "pos": f"{epoch}:{n}", "data_b64": _b64(data)}, state)
            )
        if not bridge.ring.has(n):  # кольцо ушло вперёд, пока читали, — на следующем проходе снимок
            state.pop(tid, None)
            hub.notify()
        size = (bridge.cols, bridge.rows)
        if sizes.get(tid) != size:
            sizes[tid] = size
            frames.append(_frame("size", {"term_id": tid, "cols": size[0], "rows": size[1]}, None))
    except TermGone:
        if tid in known:
            known.discard(tid)
            state.pop(tid, None)
            sizes.pop(tid, None)
            frames.append(_frame("closed", {"term_id": tid}, state))
    except TmuxError:
        pass  # снимок не удался (мост умирает или не ответил) — повтор на следующем проходе (по выводу)
    return frames
