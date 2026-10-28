"""Ф86-1 (docs/architecture.md): реплики владельца, уходящие из ядра наружу — внешней
модели сводки (`GET /digest/sources`) и соседним сессиям (`<hub_siblings>`).

- `mask` — обычная маскировка секретов хаба (`work.session.mask_secrets`, как у выдачи сессиям) и поверх неё:
  если в реплике есть слово-признак (пароль, креды, токен, ключ, логин …), все слова от 6 символов, где есть
  и буквы, и цифры, и все e-mail -> `[скрыто]`;
- `squash` — пустые реплики (после `strip`) выпадают, подряд идущие одинаковые тексты одной сессии в пределах
  5 минут — одной строкой (первой)."""

import re

from hub.work import session as work_session

HIDDEN = "[скрыто]"
REPEAT_MS = 5 * 60 * 1000
# Слово-признак — началом слова, без учёта регистра («Пароль:», «креды», «token=…», «ключи»).
_HINT_RE = re.compile(r"(?<!\w)(пароль|password|кред|токен|token|ключ|key|логин|login)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_WORD_RE = re.compile(r"\S{6,}")


def _mixed(word: str) -> bool:
    return any(ch.isdigit() for ch in word) and any(ch.isalpha() for ch in word)


def mask(text: str) -> str:
    out = work_session.mask_secrets(str(text))
    if not _HINT_RE.search(out):
        return out
    out = _EMAIL_RE.sub(HIDDEN, out)
    return _WORD_RE.sub(lambda m: HIDDEN if _mixed(m.group(0)) else m.group(0), out)


def squash(items: list[dict]) -> list[dict]:
    """`items` — по времени, у каждой `session_id`, `ts`, `text` (уже маскированный). Пустые выпадают; повтор
    того же текста той же сессии не позже 5 минут после прошлого такого — выпадает."""
    out: list[dict] = []
    last: dict[int, tuple[str, int]] = {}
    for item in items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        sid, ts = item["session_id"], int(item["ts"])
        prev = last.get(sid)
        if prev is not None and prev[0] == text and ts - prev[1] <= REPEAT_MS:
            continue
        last[sid] = (text, ts)
        out.append({**item, "text": text})
    return out
