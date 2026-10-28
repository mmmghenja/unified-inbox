"""Ф78-1 (docs/architecture.md, A3–A4): задач по «Го» нет. Политика `work` проекта
читается сессиям хаба без исключений; виды карточек задач (`work_request`, `proposal`), которые ещё может
назвать агент владельца прежней версии в своих действиях, ядро и каркас агента молча отбрасывают."""

import json

import pytest

from hub.core import hub_sessions
from hub.core import owner_queue as zq

EMPTY = {"stage": 1, "ask": [], "self": []}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "не json",
        "[1, 2]",
        json.dumps({"work": "x"}),
        json.dumps({"work": {"stage": 2}}),
        json.dumps({"work": {"stage": True}}),
        json.dumps({"work": {"ask": "Bash"}}),
        json.dumps({"work": {"self": [""]}}),
    ],
)
def test_work_policy_bad_data_is_empty_without_error(raw):
    assert hub_sessions.work_policy(raw) == EMPTY


def test_work_policy_good_data():
    raw = json.dumps({"work": {"stage": 1, "ask": ["mcp__x__write"], "self": ["сама"]}, "card_route": {}})
    assert hub_sessions.work_policy(raw) == {"stage": 1, "ask": ["mcp__x__write"], "self": ["сама"]}


def test_task_kinds_left_the_queue():
    for kind in ("work_request", "proposal"):
        assert kind not in zq.KINDS and kind in zq.RETIRED_KINDS
def _action(kinds):
    return {"id": "go", "label": "Сделай", "kinds": kinds, "needs_text": False}


def test_core_action_form_drops_retired_kinds():
    assert zq._action_form(_action(["proposal"])) is None  # только отжившие — действие уходит
    assert zq._action_form(_action(["work_request", "proposal"])) is None
    form = zq._action_form(_action(["proposal", "permission"]))
    assert form is not None and form["kinds"] == ["permission"]
    assert zq._action_form(_action([]))["kinds"] == []  # пусто — все виды, как раньше
    with pytest.raises(zq.OwnerError):
        zq._action_form(_action(["нет такого"]))
