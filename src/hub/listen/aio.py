"""Отмена задач службы записи, не теряющая отмену вызывающего.

Прежний приём `task.cancel(); with suppress(CancelledError): await task` глотал отмену ВНЕШНЕЙ задачи, если
она приходила, пока та ждала дочернюю: цикл записи не останавливался и крутил перезапуски без конца (в тесте
падающего ffmpeg — до 19,5 ГБ памяти на записях журнала и подменных процессах, 14.10). Здесь дочерние задачи
и остановка процессов доводятся до конца, а отмена вызывающего пробрасывается после."""

import asyncio
from collections.abc import Awaitable, Iterable


async def finish(aw: Awaitable):
    """Довести `aw` до конца даже при отмене вызывающего; отмену вызывающего пробросить после."""
    fut = asyncio.ensure_future(aw)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(fut)
            break
        except asyncio.CancelledError:
            if fut.done():
                raise  # отменили саму `aw`, а не вызывающего
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def cancel_and_wait(tasks: Iterable[asyncio.Task | None]) -> None:
    """Отменить задачи и дождаться их конца (их исключения — не наши); отмена вызывающего не теряется."""
    pending = [task for task in tasks if task is not None]
    for task in pending:
        task.cancel()
    if pending:
        await finish(asyncio.gather(*pending, return_exceptions=True))
