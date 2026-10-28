"""Ф11-5, К2: процесс коннектора пишет info-строки хаба в журнал, сторонние библиотеки — только warning+."""

import logging

from hub import cli as hub_cli


def test_k2_connector_logging_info_for_hub_warning_for_noisy(monkeypatch) -> None:
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])  # как под systemd: обработчиков нет, basicConfig сработает
    saved_root = root.level
    saved = {name: logging.getLogger(name).level for name in hub_cli._CONNECTOR_NOISY_LOGGERS}
    try:
        hub_cli._setup_connector_logging()
        assert logging.getLogger("hub.connectors.media").isEnabledFor(logging.INFO)
        for name in ("telethon", "httpx", "websockets"):
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
            assert logging.getLogger(name).isEnabledFor(logging.WARNING)
    finally:
        root.setLevel(saved_root)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
