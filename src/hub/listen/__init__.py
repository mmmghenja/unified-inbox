"""Служба записи интервью `hub-listen` и её команды (Ф60-1, docs/architecture.md).

`files.py` — где лежат токен и данные службы; `commands.py` — `hub listen setup|hint|status` (часть A).
Часть B — `hub listen run`: `service.py` (опрос ядра, запись, перезапуски), `audio.py` (pactl, эхоподавление
PipeWire, ffmpeg), `segment.py` (нарезка по паузам, чистая логика), `core_api.py` (клиент ручек `/listen/*`),
`aio.py` (отмена задач без потери отмены)."""
