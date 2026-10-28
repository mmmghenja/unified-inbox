-- Голосовые из браузера (Ф14-4, docs/architecture.md). Общая схема (main и
-- work), как 0018: загрузка лежит в файле своего канала. Только новая колонка, без пересчётов.
--
-- `kind` — вид загрузки, если он НЕ выводится из байтов: пишется только `voice` (`POST /uploads?voice=1`,
-- запись webm/ogg из UI). `NULL` — прежнее вычисление `uploads.kind_of(detected, size)` (`photo`/`document`).
ALTER TABLE upload ADD COLUMN kind TEXT;
