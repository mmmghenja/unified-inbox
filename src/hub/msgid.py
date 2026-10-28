"""Свой Message-ID исходящего письма — общий для коннектора почты и ядра (Ф12-1 П4: ядро само знает Message-ID
черновика новой беседы, чтобы привязать её к треду по копии письма). Одна функция на обе стороны, не копия.
Там же — проверка темы нового письма (ревью Ф12-1): ядро отвечает 422, коннектор — `permanent`.
"""

import unicodedata


def subject_problem(subject: str) -> str | None:
    """Причина, по которой тему нельзя поставить в заголовок письма, или `None`. Любой символ категории
    Unicode `C*` (управляющие, форматные, суррогаты, неназначенные) и любой разделитель строк
    (`str.splitlines`: U+2028/U+2029, NEL, VT, FF, …) — отказ: сборщик письма на них падает или режет
    заголовок."""
    if len(subject.splitlines()) != 1 or subject.endswith(("\r", "\n")):
        return "тема письма — одной строкой, без переводов строк"
    if any(unicodedata.category(ch).startswith("C") for ch in subject):
        return "тема письма — без управляющих и невидимых символов"
    return None


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def our_message_id(send_token: str, address: str) -> str:
    """E2/план 1.2: детерминирован из `send_token` — повтор отправки даёт тот же Message-ID."""
    return f"<hub-{send_token}@{domain_of(address) or 'hub'}>"
