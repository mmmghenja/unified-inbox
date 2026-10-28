"""Журнал запусков Claude (`agent_run`): общие помощники. Спека `docs/architecture.md`.

С Ф57-2 запуски дежурной не пишутся (ручек `POST`/`PATCH /agent/runs` нет): строки заводят сессии хаба
(`hub_sessions.py`, `held_work.py`), `GET /agent/runs` читает главная. Старые строки с видом `digest`
читаются и показываются как есть.

Файл строки — `work`, если в проекте есть хоть одна беседа work-аккаунта (итог Claude по такому проекту
пересказывает корпоративную переписку — граница main/work, план 11.4), иначе `main`.
"""

RESULT_MAX = 4000
FAILURE_MAX = 1000


def schema_for_project(conn, project_id: int) -> str:
    """`work`, если в проекте есть беседа work-аккаунта (беседы work-аккаунтов живут в `work.channel`)."""
    row = conn.execute("SELECT 1 FROM work.channel WHERE project_id = ? LIMIT 1", (project_id,)).fetchone()
    return "work" if row is not None else "main"
