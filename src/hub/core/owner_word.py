"""Слово владельца в разговоре с агентом (Ф75-1, docs/architecture.md).

Агент владельца отвечает по карточке очереди или берёт её на «Реши» вне своего канала (маршрут «Мне» по таблице
проекта, вернувшаяся владельцу карточка), только когда действует по реплике владельца в разговоре «Мой агент»: слово
владельца в разговоре = его клик (третье исключение CLAUDE.md). Агент передаёт `word: {talk_id}` — id реплики, по
которой действует; ядро сверяет её здесь, ОДНОЙ функцией `check` (Ф76-1 добавит в `word` свои поля — сюда же).

Условия: реплика есть и её автор `owner_channel` (не агент); написана не раньше карточки; ветка реплики — «Общее»
или сессия хаба этой карточки; реплике не больше `AGE_MAX_MS`; по одной реплике открыто не больше `CARDS_MAX`
разных карточек (ответы и «Реши» по слову считаются по аудиту, миграций нет). Отказ — 403 `word_rejected: …`.
Прочие проверки ответа (пределы, страховки ботов, рабочий день, ручные сессии) — как у клика владельца, их делает
вызывающий код (`owner_queue.answer`, `errand_create`) и функции ядра дальше; здесь они не ослабляются.

Запись — нет: функция только читает; аудитные поля (`word_talk_id`) возвращает вызывающему.

Ф76-1 (П1): второй вид слова — `word: {errand_id}` (только в ответе по карточке): открытое поручение «Реши»,
которое владелец дал по ЭТОЙ карточке (и переехавшее с неё) своим текстом-указанием. Указание — его слово: строка
журнала решений `agent_on_word` с этим текстом. Поле аудита — `word_errand_id`. Текст слова для журнала —
`text_of` (тоже только чтение)."""

import json

from .owner_queue import OwnerError

TEXT_MAX = 2000  # Ф76-1: текст слова в журнале решений

WORD_REJECTED = "word_rejected"
AGE_MAX_MS = 30 * 60 * 1000
CARDS_MAX = 3
# Аудит, по которому считается, сколько карточек открыла реплика: ответ агента и поручение «Реши».
_COUNTED = ("owner_answer", "owner_errand")


def _reject(why: str) -> OwnerError:
    return OwnerError(403, f"{WORD_REJECTED}: {why}")


def _talk_row(conn, talk_id: int) -> tuple | None:
    return conn.execute(
        "SELECT ts, author, session_id FROM main.owner_talk WHERE id = ?",
        (talk_id,),
    ).fetchone()


def _opened_refs(conn, talk_id: int, since_ts: int) -> set[str]:
    rows = conn.execute(
        "SELECT json_extract(detail_json, '$.ref') FROM main.audit "
        "WHERE action IN (?, ?) AND ts >= ? AND json_extract(detail_json, '$.word_talk_id') = ?",
        (*_COUNTED, since_ts, talk_id),
    ).fetchall()
    return {r[0] for r in rows if isinstance(r[0], str)}


def check(
    conn,
    word: dict,
    *,
    ref: str,
    session_id: int | None,
    created_ts: int | None,
    now_ms: int,
    errand_ok: bool = False,
) -> dict:
    """Сверить слово владельца для карточки `ref` (сессия хаба `session_id`, создана `created_ts`) -> поля аудита
    (`{"word_talk_id": id}` / `{"word_errand_id": id}`). Отказ — `OwnerError(403, "word_rejected: …")`.
    `errand_ok` — принимать `{errand_id}` (Ф76-1; ответ по карточке, не «Реши»)."""
    if not isinstance(word, dict) or len(word) != 1:
        raise _reject("слово — одно поле: talk_id или errand_id")
    if "errand_id" in word:
        if not errand_ok:
            raise _reject("указание к «Реши» — только в ответе по карточке")
        return _check_errand(conn, word["errand_id"], ref=ref, created_ts=created_ts)
    talk_id = word.get("talk_id")
    if not isinstance(talk_id, int) or isinstance(talk_id, bool) or talk_id < 1:
        raise _reject("нет id реплики владельца")
    row = _talk_row(conn, talk_id)
    if row is None:
        raise _reject("реплики нет")
    ts, author, branch = row
    if author != "owner_channel":
        raise _reject("реплика не владельца")
    if ts is None or now_ms - ts > AGE_MAX_MS:
        raise _reject("реплика старше 30 минут")
    if created_ts is None or ts < created_ts:
        raise _reject("реплика написана раньше карточки")
    if branch is not None and branch != session_id:
        raise _reject("реплика из ветки другой сессии")
    opened = _opened_refs(conn, talk_id, ts)
    opened.discard(ref)
    if len(opened) >= CARDS_MAX:
        raise _reject(f"по одной реплике — не больше {CARDS_MAX} карточек")
    return {"word_talk_id": talk_id}


def _errand_row(conn, errand_id) -> tuple | None:
    if not isinstance(errand_id, int) or isinstance(errand_id, bool) or errand_id < 1:
        return None
    return conn.execute(
        "SELECT created_ts, closed_ts, payload_json FROM main.owner_item WHERE id = ? AND kind = 'errand'",
        (errand_id,),
    ).fetchone()


def _payload(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _check_errand(conn, errand_id, *, ref: str, created_ts: int | None) -> dict:
    """Ф76-1: `{errand_id}` — открытое поручение владельца (не авторешение агента) по этой карточке (или
    переехавшее с неё) с текстом-указанием, данное не раньше карточки."""
    row = _errand_row(conn, errand_id)
    if row is None:
        raise _reject("поручения нет")
    given_ts, closed_ts, raw = row
    payload = _payload(raw)
    if payload.get("auto") is True:
        raise _reject("поручение взял сам агент, это не указание владельца")
    if ref not in (payload.get("target"), *(payload.get("prev_targets") or [])):
        raise _reject("указание к другой карточке")
    if closed_ts is not None:
        raise _reject("поручение закрыто")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _reject("в указании владельца нет текста")
    if created_ts is None or given_ts < created_ts:
        raise _reject("указание дано раньше карточки")
    return {"word_errand_id": errand_id}


def text_of(conn, audit: dict) -> tuple[str | None, dict | None]:
    """Ф76-1: (текст слова владельца, `word_json` журнала) по полям аудита `check`; не слово — `(None, None)`.
    Только чтение."""
    talk_id = audit.get("word_talk_id")
    if isinstance(talk_id, int):
        row = conn.execute("SELECT text FROM main.owner_talk WHERE id = ?", (talk_id,)).fetchone()
        text = row[0] if row is not None and isinstance(row[0], str) else None
        return (text[:TEXT_MAX] if text else None), {"talk_id": talk_id}
    errand_id = audit.get("word_errand_id")
    if isinstance(errand_id, int):
        row = _errand_row(conn, errand_id)
        text = _payload(row[2]).get("text") if row is not None else None
        return (text[:TEXT_MAX] if isinstance(text, str) else None), {"errand_id": errand_id}
    return None, None
