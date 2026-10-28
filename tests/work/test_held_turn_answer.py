"""Ф56-1б (docs/architecture.md): последний ответ хода и снимок общего текста.

- `turn_answer`: склейка блоков, нормализация переносов, «ВЛАДЕЛЬЦУ:» в разных написаниях (последняя строка
  со строками до первой пустой), хвост ответа, обрезка 64 000;
- раннер копит тексты хода: вызов инструмента сбрасывает, шаги подагентов — мимо, начало хода очищает;
- сквозь хост и ядро: ход с «ВЛАДЕЛЬЦУ:» после вызова инструмента -> карточка `turn` с кратким и полным;
- `sys_pin` несёт `common_sha`; `pin_fix` даёт раздел «Итог хода» при отличии и при отсутствии ключа."""

import asyncio
import hashlib
import json

from hub import roles
from hub.work import held as held_mod
from hub.work.runner_base import BaseRunner

from .test_held_host import _running, _session, _turn_done

# --- turn_answer -------------------------------------------------------------------------------------------


def test_turn_answer_none_without_text():
    assert held_mod.turn_answer([]) is None
    assert held_mod.turn_answer(["", "  \n"]) is None


def test_turn_answer_glues_blocks_and_takes_tail():
    out = held_mod.turn_answer(["Первый\r\nблок", "Второй\rблок\n\nПоследний абзац  "])
    assert out == {
        "full": "Первый\nблок\n\nВторой\nблок\n\nПоследний абзац",
        "brief": "Последний абзац",
        "brief_kind": "tail",
        "cut": False,
    }
    long = held_mod.turn_answer(["а" * 500])
    assert long["brief"] == "а" * 299 + "…" and long["brief_kind"] == "tail"


def test_turn_answer_owner_line_forms():
    for line in (
        "ВЛАДЕЛЬЦУ: дай доступ",
        "владельцу: дай доступ",
        "**ВЛАДЕЛЬЦУ:** дай доступ",
        "**владельцу**: дай доступ",
    ):
        out = held_mod.turn_answer([f"Сделал.\n\n{line}\nк серверу X\n\nP.S. хвост"])
        assert out["brief_kind"] == "owner_channel", line
        assert out["brief"] == "дай доступ\nк серверу X", line
    # Последняя строка «ВЛАДЕЛЬЦУ:» — главнее прежних.
    out = held_mod.turn_answer(["ВЛАДЕЛЬЦУ: первое", "ВЛАДЕЛЬЦУ: второе"])
    assert out["brief"] == "второе"
    # Строка «ВЛАДЕЛЬЦУ:» без текста — краткое из абзаца перед ней, вид всё равно `owner_channel` (просит карточку).
    out = held_mod.turn_answer(["Готово, ветка f1\n\nВЛАДЕЛЬЦУ:"])
    assert out["brief_kind"] == "owner_channel" and out["brief"] == "Готово, ветка f1"
    # Слово «владельцу» посреди строки — не строка для владельца.
    assert held_mod.turn_answer(["написал владельцу: всё ок"])["brief_kind"] == "tail"


def test_turn_answer_cut_after_brief():
    text = "x" * 70_000 + "\n\nВЛАДЕЛЬЦУ: проверь"
    out = held_mod.turn_answer([text])
    assert out["cut"] is True and len(out["full"]) == 64_000 and out["full"].endswith("…")
    assert out["brief"] == "проверь" and out["brief_kind"] == "owner_channel"  # краткое — по тексту до обрезки


# --- раннер копит тексты хода ------------------------------------------------------------------------------


class _Host:
    def monotonic(self) -> float:
        return 0.0


class _Runner(BaseRunner):
    rich = True

    def __init__(self) -> None:
        self._init_base(_Host(), workdir="/tmp/p", engine="claude")  # noqa: S108 — путь не трогается
        self.events: list[tuple] = []
        self.began = 0

    def event(self, kind, summary, **kw) -> None:
        self.events.append((kind, summary))

    async def turn_began(self) -> None:
        self.began += 1


def _assistant(*blocks, parent=None) -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {"id": "m", "content": list(blocks)},
    }


def _text(text) -> dict:
    return {"type": "text", "text": text}


def _tool() -> dict:
    return {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}


def test_runner_keeps_texts_after_last_tool_only():
    async def go():
        runner = _Runner()
        await runner.on_message(_assistant(_text("смотрю")))
        await runner.on_message(_assistant(_tool()))
        await runner.on_message(_assistant(_text("Готово.")))
        await runner.on_message(_assistant(_text("чужое"), parent="t1"))  # шаг подагента — мимо
        await runner.on_message(_assistant(_text("ВЛАДЕЛЬЦУ: проверь")))
        assert runner.turn_texts == ["Готово.", "ВЛАДЕЛЬЦУ: проверь"]
        assert runner.began == 1
        # Новый ход начинается с чистого списка.
        runner.busy = False
        await runner.on_message(_assistant(_text("новый ход")))
        assert runner.turn_texts == ["новый ход"] and runner.began == 2

    asyncio.run(go())


# --- сквозь хост и ядро ------------------------------------------------------------------------------------


async def test_turn_answer_reaches_core_as_turn_card(env):
    env.scenario(
        turns=[
            [
                {"text": "смотрю"},
                {"tool": "Bash", "input": {"command": "ls"}, "result": "a"},
                {"text": "Готово, ветка f1."},
                {"text": "**ВЛАДЕЛЬЦУ:** нужен ключ API\nот стенда"},
            ]
        ]
    )
    host = await env.started()
    sid, _pid, _workdir = await _session(env, role="программист")
    await _running(env, host, sid, "сделай")
    await _turn_done(env, host, sid)
    r = await env.ui("GET", "/owner_channel/queue")
    assert r.status_code == 200, r.text
    cards = [c for c in r.json()["items"] if c["kind"] == "turn"]
    assert len(cards) == 1
    card = cards[0]
    assert card["ref"] == f"turn:{sid}:1" and card["brief"] == "нужен ключ API\nот стенда"
    assert card["full"]["answer"] == "Готово, ветка f1.\n\n**ВЛАДЕЛЬЦУ:** нужен ключ API\nот стенда"
    assert card["full"]["by_ui_say"] is True
    with env.db.reader() as conn:
        payload = json.loads(conn.execute("SELECT payload_json FROM main.owner_item").fetchone()[0])
    assert payload["brief_kind"] == "owner_channel"


# --- снимок общего текста (3.7) ----------------------------------------------------------------------------


def _pin(role="программист", **extra) -> dict:
    return held_mod.sys_pin({"mode": "manual", "role": role, "role_no": 1, **extra}, None, None)


def test_sys_pin_has_common_sha_only_with_role():
    sha = hashlib.sha256(roles.common_text().encode("utf-8")).hexdigest()
    assert _pin()["common_sha"] == sha
    assert "common_sha" not in _pin(role=None)  # без роли ключа нет — снимок как до 56-1б


def test_pin_fix_adds_turn_section_when_common_changed_or_unknown():
    now = _pin()
    assert held_mod.pin_fix(dict(now), now, None) is None
    section = held_mod.common_section()
    assert section.startswith("Итог хода\n") and "ВЛАДЕЛЬЦУ:" in section
    for pinned in ({**now, "common_sha": "0" * 64}, {k: v for k, v in now.items() if k != "common_sha"}):
        fix = held_mod.pin_fix(pinned, now, None)
        assert fix is not None and fix.startswith(held_mod.PIN_FIX_HEAD)
        assert held_mod.PIN_COMMON_CHANGED + section in fix
        assert "Кто ставит задачи" not in fix  # только раздел «Итог хода», не весь файл
    # Сессия без роли: общего текста в системном нет — поправки нет.
    roleless = _pin(role=None)
    assert held_mod.pin_fix({k: v for k, v in roleless.items() if k != "common_sha"}, roleless, None) is None
    # Снимка нет (сессия до Ф48-5) — поправок нет, как было.
    assert held_mod.pin_fix(None, now, None) is None
