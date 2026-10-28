"""Очередь канала владельца (Ф56-1а, docs/architecture.md; план фазы — П1–П3, П8–П11, П14, П17, А5,
А9–А11; раздел 8 плана главнее 7).

Одна функция ядра (`items`) собирает всё, что у сессий хаба и проектов ждёт владельца, в единый список карточек;
одна (`answer`) отвечает по карточке теми же функциями ядра, что и кнопки владельца в интерфейсе. Кто что видит:
кука интерфейса — всё (и карточки `in_channel=false`); агент владельца (bearer ровно со скоупом `human`) — только
карточки «в канале» проектов своего токена. Ответ агента оставляет строку `owner_item` с `closed_by='agent'`
(источник ответа, П8); автору и ленте он выглядит как ответ владельца.

Заявочные карточки своих строк не имеют: считаются при чтении из таблиц-источников. Формат карточки один на
все виды (56-1б/в добавят виды, не поля). Тексты карточек — чужой ввод: через маскировку (`card_view`,
`work.session.mask_secrets`), длинное — с обрезкой.

Ф66-1 (docs/architecture.md): «в канале» (у агента) — по таблице маршрута проекта
(`card_route`): группа вида `agent` и сессия-автор не ручная сессия хаба. Агенту отдаются ещё карточки с
открытым поручением «Реши» (П2). Делегат Ф49 убран.

Кнопки агента (П17): объявленные агентом действия лежат в `main.meta` (`owner_agent_actions`); поручение
агенту — строка `owner_item` вида `errand`.

Ф56-1б (docs/architecture.md; план фазы — П4–П7, А4, А6, А7): вид `turn` — сессия хаба
закончила ход и ждёт владельца (своя строка `owner_item`, заводит `held_work.turn` через `open_turn_card`; ответ —
реплика сессии `held_say`), вид `agent_msg` — сообщение агента владельцу (`peer_msg.to_user`, в том числе вопрос
`hub_ask_owner`; ответ — сообщение «ОТ ВЛАДЕЛЬЦА» автору).

Ф56-1в (docs/architecture.md; план фазы — П12, А1–А3, А8): состояния сессии хаба — свои
строки `owner_item`, по одной открытой каждого вида на сессию: `stall` (стоит без дела — отменена 22.10,
`retire_stalls` закрывает старые), `broken` (ход оборван усыплением извне — `held_work.force_asleep` и обрыв
хабом), `dead` (не запускается — третья неудача запуска подряд), `stuck` (ход идёт, шагов нет — докладывает
хост).

Запись — только внутри `Db.write()`, время — параметром `now_ms`."""

import json
import re
from typing import NamedTuple

from . import agent_requests as agent_requests_mod
from . import card_route as card_route_mod
from . import decisions as decisions_mod
from . import engine as engine_mod
from . import engine_fallback as engine_fallback_mod
from . import group_ops as group_ops_mod
from . import held as held_mod
from . import held_signals as held_signals_mod
from . import held_work as held_work_mod
from . import hub_sessions as hub_sessions_mod
from . import human_requests as human_mod
from . import join as join_mod
from . import leave as leave_mod
from . import machines as machines_mod
from . import outbox as outbox_mod
from . import outbox_ops, queries
from . import peer_msgs as peer_msgs_mod
from . import presses as presses_mod
from . import reminders as reminders_mod
from . import rule_requests as rule_requests_mod
from . import subs as subs_mod
from . import workday as workday_mod
from .audit import write_audit
from .db import db_of, next_seq
from .hub_sessions import SessionError
from .security import HUMAN_SCOPES, Principal

# Подпись поручения, которое агент взял себе сам (авторешение).
AUTO_LABEL = "решаю сам"
# Ф75-1: «Реши», запущенный агентом по слову владельца в разговоре (`word`, `owner_word.check`).
WORD_LABEL = "по твоему слову в разговоре"

# Виды карточек этого куска — в порядке очереди (как «Ждёт тебя» интерфейса: сначала то, что держит сессию).
KINDS = (
    "permission",
    "question",
    "human",
    "draft",
    "press",
    "join",
    "leave",
    "group_op",
    "rule",
    "agent_request",
    "reminder",
    "heavy",
    "paused",
    "interrupted",
    "turn",
    "broken",
    "dead",
    "stuck",
    "stall",
    "agent_msg",
)
# Ф56-1в: виды состояний сессии хаба (своя строка `owner_item`, `ref = <вид>:<сессия>:<номер хода>`).
STATE_KINDS = ("broken", "dead", "stuck", "stall")
# Виды со своей строкой `owner_item` — ответ агента её закрывает, отдельной строки «ответ агента» нет.
_OWN_ROW_KINDS = ("turn", *STATE_KINDS)
ERRAND_KIND = "errand"
_ORDER = {kind: n for n, kind in enumerate(KINDS)}

# Приставка `ref` -> вид(ы). `req:` — карточка сессии хаба: разрешение или вопрос (по самой карточке).
_PREFIX = {
    "req": None,
    "human": "human",
    "draft": "draft",
    "press": "press",
    "join": "join",
    "leave": "leave",
    "group_op": "group_op",
    "rule": "rule",
    "agent_request": "agent_request",
    "reminder": "reminder",
    "heavy": "heavy",
    "paused": "paused",
    "say": "interrupted",
    "turn": "turn",
    "agent_msg": "agent_msg",
    **{kind: kind for kind in STATE_KINDS},
}
_REF_RE = re.compile(r"^([a-z_]+):(\d{1,16})$")
# Ф56-1б (3.1): `turn:<сессия хаба>:<номер хода>` — номер хода ведёт ядро (`hub_session.turns`). Ф56-1в:
# так же устроены `ref` состояний сессии (`stall`, `broken`, `dead`, `stuck`).
_TURN_REF_RE = re.compile(r"^(turn|stall|broken|dead|stuck):(\d{1,16}):(\d{1,16})$")
_ID_MAX = 2**53

BRIEF_MAX = 300
TEXT_MAX = held_work_mod.MESSAGE_MAX  # текст ответа «отказать», примечание отказа заявки
PROGRESS_MAX = 200
RETURN_MAX = 200
ERRAND_TEXT_MAX = 2000
CLOSED_MAX = 200
DRAFTS_MAX = 200
# Ф56-1б: краткое карточки `turn` (строки «ВЛАДЕЛЬЦУ:» сессии — до трёх строк, переносы сохраняются), полный
# последний ответ (без предела 8000 событий ленты; предел здравого смысла — как у хоста), краткое `agent_msg`.
TURN_BRIEF_MAX = 1000
TURN_FULL_MAX = 64_000
TURN_SUMMARY_MAX = held_work_mod.SUMMARY_MAX
TURN_NO_ANSWER = "ход окончен без ответа"
TURN_BRIEF_KINDS = ("owner_channel", "tail")
# 23.10 (вопрос: что, если модель забудет «ВЛАДЕЛЬЦУ:», когда ждёт ответа?): у «сразу да» ответ без «ВЛАДЕЛЬЦУ:»,
# два последних абзаца которого (без блоков кода, ссылок и цитат «>») всё же просят владельца, карточку даёт —
# агенту. «Сейчас ничего не нужно» в тех же абзацах — не просьба. Правило отсеивает почти все такие ответы
# и оставляет те, где в конце вопрос или просьба.
_TURN_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_TURN_URL = re.compile(r"https?://\S+")
_TURN_NOTHING = re.compile(
    r"ничего не (?:нужно|требуется|жду)|(?:сейчас|сделать сейчас|от тебя)\W{0,4}ничего", re.I
)
_TURN_ASK = re.compile(
    r"\?|тво(?:ё|е|его|ей|я|ю) (?:слов|решени|ответ|подтвержд|разрешени|выбор|го\b)"
    r"|тво(?:й|его) (?:ответ|выбор)"
    r"|подтверди|разреши|за тобой|реши,|выбери|скажи,|напиши, (?:если|как|что|какой)|жду от тебя"
    r"|нужн\w* (?:от тебя|твоё|твое|твой)",
    re.I,
)
AGENT_BRIEF_MAX = 200
# Ф56-1в: третья неудача запуска подряд — хост больше не пробует (А2).
LAUNCH_FAILS_DEAD = held_work_mod.MAX_LAUNCH_FAILS
NOTE_CUT = "ход оборван хабом (ограничение автопилота)"
STATE_CLOSED = "по карточке уже ответили или она снята"

# П17: объявленные действия агента.
AGENT_META = "owner_agent_actions"
ACTIONS_MAX = 20
ACTION_ID_RE = re.compile(r"[a-z0-9_-]{1,32}")
ACTION_LABEL_MAX = 60
# «На связи» — агент опрашивал очередь не дольше этого назад.
ONLINE_MS = 60_000
# Опрос очереди агентом пишет отметку не чаще этого (каждый опрос — не нужно: «на связи» — минута).
SEEN_WRITE_MS = 5_000

NOT_IN_CHANNEL = "not_in_channel"


class OwnerError(Exception):
    """Отказ очереди: `status` — HTTP-код, `detail` — готовый текст."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# Ф65-1 (Ж1): ответ владельца (кука, бот) по карточке с открытым поручением агенту поручение закрывает (`ui`) —
# 409 `errand_open` Ф58-3 больше нет. Причины возврата, которые пишет ядро (Ж1–Ж3).
TAKEN_BY_OWNER = "ответил владелец"
STOPPED_BY_OWNER = "остановил владелец"
# Ф65-1 (Ж2): поручение без нового шага дольше этого ядро возвращает владельцу (`closed_by='system'`).
HUNG_MS = 300_000
RETURN_DETAIL_MAX = 2000
RESULT_MAX = 300
# Ф65-1 (Ж6): авторешение вида, снятого владельцем в «Мой агент берёт сам», — 409 с этим кодом.
AUTO_OFF_CODE = "auto_off"
AUTO_OFF = f"{AUTO_OFF_CODE}: этот вид карточек агент сам не берёт — только по «Реши»"
# Ф65-1 (Ж7): вернутую карточку агент сам снова не берёт — только «Реши» или текст владельца.
AUTO_RETURNED_CODE = "auto_returned"
AUTO_RETURNED = f"{AUTO_RETURNED_CODE}: карточка возвращена владельцу — снова только по «Реши» или его слову"

# Ф96-1 (docs/architecture.md): руки посреди поручения агента. Поручение остаётся открытым;
# `hands` в его `payload_json` — у кого сейчас управление окном: `agent` (ключа нет) или `owner_channel`.
HANDS = ("owner_channel", "agent")
HANDS_MAX_MS = 2 * 60 * 60_000  # руки у владельца дольше — поручение снимается (A.4)
HANDS_BACK_MS = 30 * 60_000  # П1: срок заявки «нужен человек» после возврата рук агенту — не меньше
HANDS_WHY_MAX = 500
HANDS_NOTE_MAX = 2000
HANDS_NOTE_DEFAULT = "владелец вернул управление"
HANDS_HUNG = f"руки у владельца больше {HANDS_MAX_MS // 3_600_000} ч — поручение снято"
HANDOFF_PREFIX = "Передаю тебе: "
HANDS_STALE_CODE = "stale"
HANDS_STALE = f"{HANDS_STALE_CODE}: кнопка устарела — руки уже передавали"
HANDOFF_KEEP = 50  # номеров реплик «Передаю тебе» в одном поручении


def _answer_spec(action: str, label: str, *, text: str = "none", answers: bool = False) -> dict:
    """Допустимый ответ карточки: `needs_text` — текст обязателен, `takes_text` — текст можно приложить,
    `needs_answers` — нужны ответы на вопросы (`answers` `{вопрос: ответ}`)."""
    return {
        "action": action,
        "label": label,
        "needs_text": text == "required",
        "takes_text": text != "none",
        "needs_answers": answers,
    }


_ALLOW = _answer_spec("allow", "Разрешить")
_DENY = _answer_spec("deny", "Отказать", text="optional")
_ANSWER = _answer_spec("answer", "Ответить", answers=True)
_APPROVE_DECLINE = {
    "press": ("Нажать", "Не нажимать"),
    "join": ("Вступить", "Не вступать"),
    "leave": ("Выйти", "Оставить"),
    "group_op": ("Да", "Отказать"),  # Ф62-6
    "rule": ("Завести", "Отклонить"),
}

ANSWERS: dict[str, list[dict]] = {
    "permission": [_ALLOW, _DENY],
    "question": [_ANSWER, _DENY],
    "human": [
        # Ф93-2 (A): текст ответа — причина исхода заявки (`decided_note`, до `human_requests.NOTE_MAX`).
        _answer_spec("done", "Готово", text="optional"),
        _answer_spec("failed", "Не вышло", text="optional"),
        _answer_spec("later", "Не сейчас", text="optional"),
    ],
    # `approve` — с правкой текста (`text`) и «Отправить сейчас» (`send_now`).
    "draft": [_answer_spec("approve", "Отправить", text="optional"), _answer_spec("cancel", "Отклонить")],
    **{
        kind: [_answer_spec("approve", yes), _answer_spec("decline", no)]
        for kind, (yes, no) in _APPROVE_DECLINE.items()
    },
    "agent_request": [
        _answer_spec("approve", "Одобрить"),
        _answer_spec("decline", "Отклонить", text="optional"),
    ],
    "reminder": [_answer_spec("done", "Закрыть")],
    "heavy": [_answer_spec("wake", "Поднять")],
    "paused": [_answer_spec("wake", "Отправить всё равно")],
    "interrupted": [_answer_spec("resend", "Отправить снова"), _answer_spec("drop", "Убрать")],
    # Ф56-1б (3.4, 4.1): ответ — свободный текст (реплика сессии / сообщение «ОТ ВЛАДЕЛЬЦА» автору).
    "turn": [_answer_spec("reply", "Ответить", text="required"), _answer_spec("ok", "Принял")],
    "agent_msg": [_answer_spec("reply", "Ответить", text="required"), _answer_spec("ok", "Прочитал")],
    # Ф56-1в: состояния сессии хаба.
    "stall": [_answer_spec("say", "Написать", text="required"), _answer_spec("hold", "Пусть ждёт")],
    "broken": [
        _answer_spec("wake", "Поднять и продолжить"),
        _answer_spec("say", "Написать", text="required"),
        _answer_spec("sleep", "Пусть спит"),
    ],
    "dead": [_answer_spec("retry", "Попробовать снова"), _answer_spec("say", "Написать", text="required")],
    "stuck": [
        _answer_spec("stop", "Стоп"),
        _answer_spec("interrupt", "Прервать и сказать…", text="required"),
        _answer_spec("wait", "Пусть работает"),
    ],
}
# Предел текста ответа по виду (`None` — проверяет функция ядра); прочим видам — `TEXT_MAX`.
_TEXT_LIMIT = {
    "draft": None,
    "human": human_mod.NOTE_MAX,
    "turn": held_work_mod.SAY_MAX,
    "agent_msg": peer_msgs_mod.TEXT_MAX,
    **{kind: held_work_mod.SAY_MAX for kind in STATE_KINDS},
}


# Ф89-1 (П1, П5): повторяемое напоминание (подвид `repeat`) — «Сделано» (следующий срок) и «Снять» (`stop`).
REPEAT_REMINDER_ANSWERS = [_answer_spec("done", "Сделано"), _answer_spec("stop", "Снять")]

# Ф83-1 (П6): ответ карточки паузы «Перейти на <движок · модель>» — только у сессии с подходящим запасным
# (карточка показывает его сама, с подписью запасного); для проверки ответа — всегда допустим.
PAUSED_FALLBACK = _answer_spec("fallback", "Перейти на запасной")


def _answers_of(kind: str, sub: str | None, *, all_: bool = False) -> list[dict]:
    if all_ and kind == "paused":
        return [*ANSWERS["paused"], PAUSED_FALLBACK]
    if kind == "reminder" and sub == card_route_mod.REPEAT_SUB:
        return REPEAT_REMINDER_ANSWERS
    return ANSWERS.get(kind, [])


# --- маскировка ---------------------------------------------------------------------------------------------


def _mask(text) -> str:
    from ..work import session as work_session  # как в `card_view`: код хоста, только stdlib

    return work_session.mask_secrets(str(text))


def _brief(*parts) -> str:
    """Строка «что нужно»: части через « — », маскированная, в одну строку, не длиннее `BRIEF_MAX`."""
    flat = " ".join(" — ".join(str(p) for p in parts if p not in (None, "")).split())
    flat = _mask(flat)
    return flat if len(flat) <= BRIEF_MAX else flat[: BRIEF_MAX - 1] + "…"


def _clip_text(text: str, limit: int) -> str:
    """Многострочный текст под предел: переносы сохраняются, длинное — с «…» в конце."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mask_tree(value):
    """`full` карточки: все строки — через маскировку и обрезку (начало и конец, как у делегата)."""
    if isinstance(value, str):
        return _clip_view(_mask(value))[0]
    if isinstance(value, dict):
        return {k: _mask_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_tree(v) for v in value]
    return value


# --- что видит решающий карточку сессии (из `card_delegate`, Ф66-1; П6, П7 Ф49) -----------------------------

VIEW_HEAD = 3000
VIEW_TAIL = 3000
REASON_VIEW_MAX = 2000
QUESTIONS_MAX = 20
OPTIONS_MAX = 20
# Инструменты, пишущие файл: путь и размер; тело — если файл не хранилище секретов.
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_PATH_KEYS = ("file_path", "notebook_path", "path")


def _clip_view(text: str) -> tuple[str, int]:
    """Длинный текст — начало и конец (`VIEW_HEAD` + `VIEW_TAIL`); второе значение — сколько символов
    пропущено посередине."""
    if len(text) <= VIEW_HEAD + VIEW_TAIL:
        return text, 0
    skipped = len(text) - VIEW_HEAD - VIEW_TAIL
    return f"{text[:VIEW_HEAD]}\n… пропущено {skipped} символов …\n{text[-VIEW_TAIL:]}", skipped


def _input_text(tool_input: dict, skip: tuple[str, ...] = ()) -> str:
    """Вход инструмента строками «ключ: значение» (строки — как есть, прочее — JSON)."""
    lines = []
    for key, value in tool_input.items():
        if key in skip:
            continue
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {shown}")
    return "\n".join(lines)


def _questions(tool_input: dict, mask) -> list[dict]:
    """Вопросы карточки с номерами: `{n, question, shown, options, multi}`. `question` — текст вопроса как
    есть (ключ ответа в `answers`), `shown` и `options` — маскированные, для показа."""
    raw = tool_input.get("questions")
    found: list[dict] = []
    for item in raw[:QUESTIONS_MAX] if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            continue
        options = item.get("options") if isinstance(item.get("options"), list) else []
        labels = [
            mask(o["label"])[:500]
            for o in options[:OPTIONS_MAX]
            if isinstance(o, dict) and isinstance(o.get("label"), str)
        ]
        found.append(
            {
                "n": len(found) + 1,
                "question": item["question"],
                "shown": mask(item["question"])[:2000],
                "options": labels,
                "multi": bool(item.get("multiSelect")),
            }
        )
    return found


def card_view(request: dict) -> dict:
    """Карточка разрешения или вопроса сессии (`request` — сырой словарь столбцов `held_work`). Сырого входа в
    ответе нет: `reason` и текст входа проходят `mask_secrets`; длинный текст — начало и конец с числом
    пропущенных символов. Инструмент, пишущий файл: `file` `{path, size, secret}` и тело только если файл
    не хранилище секретов (правило Ф41-3, `work.session.secret_file`). Вопрос — `questions`."""
    from ..work import session as work_session

    mask = work_session.mask_secrets
    try:
        tool_input = json.loads(request["input_json"])
    except ValueError, TypeError:
        tool_input = None
    if not isinstance(tool_input, dict):
        tool_input = {}
    view: dict = {
        "id": request["id"],
        "session_id": request["session_id"],
        "kind": request["kind"],
        "tool": request["tool"],
        "rule": request.get("rule"),
        "created_ts": request["created_ts"],
        "reason": mask(request["reason"])[:REASON_VIEW_MAX] if request.get("reason") else None,
        "input_truncated": bool(request.get("input_truncated")),
        "file": None,
        "questions": [],
    }
    skip: tuple[str, ...] = ()
    hide_body = False
    if request["tool"] in WRITE_TOOLS:
        path = next((tool_input[k] for k in _PATH_KEYS if isinstance(tool_input.get(k), str)), None)
        body = _input_text(tool_input, _PATH_KEYS)
        # Обрезанный вход хранится без ключей — пути не узнать: тело записи не показываем вовсе.
        hide_body = work_session.secret_file(path or "") or bool(request.get("input_truncated"))
        view["file"] = {"path": mask(path)[:1000] if path else None, "size": len(body), "secret": hide_body}
        skip = _PATH_KEYS
    if request["kind"] == "question":
        view["questions"] = _questions(tool_input, mask)
        skip = (*skip, "questions")
    text, skipped = ("", 0) if hide_body else _clip_view(mask(_input_text(tool_input, skip)))
    view["input_text"] = text
    view["input_skipped"] = skipped
    return view


# --- признак «в канале» (3.1, П3, А11) ----------------------------------------------------------------------


def in_channel(
    conn,
    project_id: int | None,
    hub_session_id: int | None,
    kind: str,
    sub: str | None = None,
    *,
    ref: str | None = None,
    money: bool | None = None,
) -> bool:
    """Ф66-1 (B.1): карточка «у агента» — маршрут её вида по таблице проекта `agent` и сессия-автор не ручная
    сессия хаба (`card_route.for_card`). Проект 0 и карточка без проекта — у владельца. Ф76-1 (П7): карточка `ref`
    под «всегда меня» проекта — у владельца. Ф93-1: денежная (`money`; не передан — по `ref`) — по строке
    «Деньги»."""
    return (
        card_route_mod.for_card(conn, project_id, kind, hub_session_id, sub=sub, ref=ref, money=money)
        == card_route_mod.AGENT
    )


class _Ctx:
    """Кэши одного чтения очереди: имена проектов, сессии хаба, подписки, таблицы маршрута."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM main.project")}
        self._sess: dict[int, held_mod.HubSession | None] = {}
        self._subs: dict[int, int | None] = {}
        self._sub_machine: dict[int, str | None] = {}  # Ф77-1: подписка -> показ её машины
        self.machines = machines_mod.Labels(conn)
        self._tables = card_route_mod.projects_tables(conn)
        self._always: dict[int | None, frozenset[str]] = {}  # Ф76-1 (П7): ключи «всегда меня» по проекту

    def session(self, hub_id: int | None) -> held_mod.HubSession | None:
        if hub_id is None:
            return None
        if hub_id not in self._sess:
            self._sess[hub_id] = held_mod.get(self.conn, hub_id)
        return self._sess[hub_id]

    def hub_of_sub(self, sub_id: int | None) -> int | None:
        """А9: подписка-автор -> номер сессии хаба (у внешней сессии — `None`)."""
        if sub_id is None:
            return None
        if sub_id not in self._subs:
            sub = subs_mod.get_sub(self.conn, sub_id)
            self._subs[sub_id] = held_mod.held_id(sub.session_pid) if sub is not None else None
            self._sub_machine[sub_id] = self.machines.of_rec(sub)["display"] if sub is not None else None
        return self._subs[sub_id]

    def machine_name(self, hub_id: int | None, author_sub: int | None) -> str | None:
        """Ф77-1 (A5): показ машины сессии карточки — сессия хаба на машине ядра, внешняя — на машине своей
        подписки; сессии у карточки нет — `None`."""
        if hub_id is not None:
            return self.machines.core_label()["display"]
        if author_sub is None:
            return None
        self.hub_of_sub(author_sub)
        return self._sub_machine.get(author_sub)

    def route(
        self,
        project_id: int | None,
        hub_id: int | None,
        kind: str,
        sub: str | None = None,
        ref: str | None = None,
        money: bool = False,
    ) -> str:
        """Маршрут карточки (`card_route.for_card`) по таблицам этого чтения. Ф93-1 (A.5): денежная карточка
        (`money`) — по строке «Деньги» таблицы, мимо строки своего вида (в том же месте, что у `for_card`)."""
        rec = self.session(hub_id)
        if rec is not None and rec.mode != "auto":
            return card_route_mod.ME
        if kind == "turn" and card_route_mod.asked_by_owner(self.conn, ref):
            return card_route_mod.ME
        group = card_route_mod.GROUP_OF.get(kind)
        if group is None or project_id is None or project_id == subs_mod.NO_PROJECT_ID:
            return card_route_mod.ME
        table = self._tables.get(project_id, card_route_mod.of_policy(None))
        if money:
            route = card_route_mod.money_route(table)
        else:
            route = card_route_mod.effective(table[group], kind, sub)
        if route != card_route_mod.ME and ref is not None:
            if project_id not in self._always:
                self._always[project_id] = decisions_mod.always_me_keys(self.conn, project_id)
            keys = self._always[project_id]
            if keys and decisions_mod.always_me_card(self.conn, project_id, ref, keys=keys):
                return card_route_mod.ME
        return route

    def in_channel(
        self,
        project_id: int | None,
        hub_id: int | None,
        kind: str,
        sub: str | None = None,
        ref: str | None = None,
        money: bool = False,
    ) -> bool:
        return self.route(project_id, hub_id, kind, sub, ref, money) == card_route_mod.AGENT


def _visible(principal, project_id: int | None) -> bool:
    if principal.is_ui or principal.project_ids is None:
        return True
    return project_id is not None and project_id in principal.project_ids


def _card(
    ctx: _Ctx,
    *,
    ref: str,
    kind: str,
    project_id: int | None,
    hub_id: int | None,
    brief: str,
    full: dict,
    created_ts: int,
    role: str | None = None,
    role_no: int | None = None,
    until_ts: int | None = None,
    sub: str | None = None,
    author_sub: int | None = None,
    money: bool = False,
) -> dict:
    """Карточка единого формата (раздел 4 спеки). Ключи `_…` — служебные, наружу не уходят. Ф66-1:
    `in_channel` — маршрут вида по таблице проекта (`_Ctx.in_channel`). Ф77-1: `author_sub` — подписка
    внешней сессии-автора (для `machine_name`). Ф93-1: `money` — денежная карточка (`card_route.is_money`):
    маршрут по строке «Деньги», в выдаче `money: true` (пометка интерфейса, бот)."""
    rec = ctx.session(hub_id)
    if rec is not None and rec.role is not None:
        role, role_no = rec.role, rec.role_no
    project_name = ctx.names.get(project_id) if project_id is not None else None
    return {
        "ref": ref,
        "kind": kind,
        "project_id": project_id,
        "project_name": project_name,
        "session_id": hub_id,
        "role": role,
        "role_no": role_no,
        # Ф77-1 (A5): машина сессии карточки («роль · проект · машина»); сессии нет — `null`.
        "machine_name": ctx.machine_name(hub_id, author_sub),
        "path": {"project": project_name, "head": None, "author": role},
        "brief": brief,
        "full": full,
        "created_ts": created_ts,
        "until_ts": until_ts,
        "in_channel": ctx.in_channel(project_id, hub_id, kind, sub, ref, money),
        "money": bool(money),
        "hold_ts": None,
        "answers": _answers_of(kind, sub),
        "agent_actions": [],
        "errand": None,
        "errand_returned": None,
        # Ф65-1: агент берёт карточку сам (Ж6; Ф66-1 — маршрут `agent`) и открытый вопрос агента по ней (Ж5) —
        # заполняет `items`.
        "auto_ok": True,
        "agent_ask": None,
    }


# --- сборщики по видам --------------------------------------------------------------------------------------


def _held_requests(ctx: _Ctx, now_ms: int) -> list[dict]:
    """Разрешения и вопросы сессий хаба (`held_request` pending)."""
    conn = ctx.conn
    found: list[dict] = []
    for schema in held_mod.STORES:
        rows = conn.execute(
            held_work_mod._sql(  # noqa: SLF001 — тот же список столбцов, что у карточек сессии
                schema,
                "SELECT {cols} FROM {schema}.held_request r JOIN main.hub_session s "
                "ON s.id = r.session_id AND s.store = '{schema}' "
                "WHERE r.status = 'pending' AND s.closed_ts IS NULL ORDER BY r.id",
                cols=held_work_mod._REQUEST_SELECT,  # noqa: SLF001
            )
        ).fetchall()
        requests = [dict(zip(held_work_mod._REQUEST_COLUMNS, r, strict=True)) for r in rows]  # noqa: SLF001
        for request in requests:
            rec = ctx.session(request["session_id"])
            if rec is None:
                continue
            view = card_view(request)
            if request["kind"] == "question":
                first = view["questions"][0]["shown"] if view["questions"] else None
                parts = ("вопрос", first)
            else:
                parts = (request["tool"] or request["kind"], (view["input_text"] or "").split("\n")[0])
            brief = _brief(*parts)
            found.append(
                _card(
                    ctx,
                    ref=f"req:{request['id']}",
                    kind=request["kind"],
                    project_id=rec.project_id,
                    hub_id=rec.id,
                    brief=brief,
                    full=view,
                    created_ts=request["created_ts"],
                    money=card_route_mod.is_money(request["kind"], view),
                )
            )
    return found


def _humans(ctx: _Ctx, now_ms: int) -> list[dict]:
    rows = human_mod._select(  # noqa: SLF001
        ctx.conn,
        "h.status = 'pending' AND (h.expires_ts IS NULL OR h.expires_ts > ?) ORDER BY h.id",
        (now_ms,),
    )
    found = []
    for req in rows:
        out = human_mod.human_dict(req, source=False)
        found.append(
            _card(
                ctx,
                ref=f"human:{req.id}",
                kind="human",
                project_id=req.project_id,
                hub_id=ctx.hub_of_sub(req.author_sub_id),
                brief=_brief(req.kind, req.site, req.need),
                full=_mask_tree(out),
                created_ts=req.created_ts,
                role=out.get("role"),
                role_no=req.author_role_no,
                until_ts=req.expires_ts,
                author_sub=req.author_sub_id,
                money=card_route_mod.is_money("human", {"kind": req.kind}),
            )
        )
    return found


def _drafts(ctx: _Ctx, principal, now_ms: int) -> list[dict]:
    """Черновики, ждущие подтверждения, — тем же списком, что главная (`queries.list_outbox`)."""
    conn = ctx.conn
    found = []
    for item in queries.list_outbox(
        conn, principal, status="pending_approval", limit=DRAFTS_MAX, now_ms=now_ms
    ):
        row = conn.execute(
            f"SELECT author_sub_id, created_ts FROM {db_of(item['id'])}.outbox WHERE id = ?",  # noqa: S608
            (item["id"],),
        ).fetchone()
        hub_id = ctx.hub_of_sub(row[0]) if row is not None else None
        if hub_id is None and row is not None:
            hub_id = card_route_mod.draft_hub(conn, item["id"], row[1])  # ревью Р1: сессия хаба без подписки
        found.append(
            _card(
                ctx,
                ref=f"draft:{item['id']}",
                kind="draft",
                project_id=item["project_id"],
                hub_id=hub_id,
                brief=_brief(item.get("channel_title"), item.get("text")),
                full=_mask_tree(item),
                created_ts=item["created_ts"],
                role=item.get("author_role"),
                author_sub=row[0] if row is not None else None,
            )
        )
    return found


def _presses(ctx: _Ctx) -> list[dict]:
    found = []
    for press in presses_mod._select(ctx.conn, "p.status = 'pending' ORDER BY p.id", ()):  # noqa: SLF001
        out = presses_mod.press_dict(ctx.conn, press, result=False)
        found.append(
            _card(
                ctx,
                ref=f"press:{press.id}",
                kind="press",
                project_id=press.project_id,
                hub_id=_press_hub(press, ctx.hub_of_sub(press.author_sub_id)),
                brief=_brief(f"кнопка «{press.text or ''}»", out.get("channel_title")),
                full=_mask_tree(out),
                created_ts=press.created_ts,
                role=out.get("role"),
                role_no=press.author_role_no,
                until_ts=press.created_ts + presses_mod.PENDING_TTL_MS,
                author_sub=press.author_sub_id,
            )
        )
    return found


def _pending_ids(conn, table: str) -> list[tuple[int, int | None]]:
    return conn.execute(
        f"SELECT id, hub_session_id FROM main.{table} WHERE status = 'pending' ORDER BY id"  # noqa: S608
    ).fetchall()


def _joins(ctx: _Ctx) -> list[dict]:
    found = []
    for request_id, hub_id in _pending_ids(ctx.conn, "join_request"):
        req = join_mod.get(ctx.conn, request_id)
        if req is None:
            continue
        found.append(
            _card(
                ctx,
                ref=f"join:{req.id}",
                kind="join",
                project_id=req.project_id,
                hub_id=hub_id,
                brief=_brief("вступить", req.title or req.target),
                full=_mask_tree(join_mod.request_dict(req)),
                created_ts=req.created_ts,
            )
        )
    return found


def _leaves(ctx: _Ctx) -> list[dict]:
    found = []
    for request_id, hub_id in _pending_ids(ctx.conn, "leave_request"):
        req = leave_mod.get(ctx.conn, request_id)
        if req is None:
            continue
        found.append(
            _card(
                ctx,
                ref=f"leave:{req.id}",
                kind="leave",
                project_id=req.project_id,
                hub_id=hub_id,
                brief=_brief("выйти", req.title),
                full=_mask_tree(leave_mod.request_dict(req)),
                created_ts=req.created_ts,
            )
        )
    return found


_GROUP_OP_BRIEF = {
    "create": "создать группу WhatsApp",
    "add": "добавить в группу",
    "remove": "убрать из группы",
    "link_reset": "сбросить ссылку группы",
}


def _group_ops(ctx: _Ctx) -> list[dict]:
    """Ф62-6: операции над группами WhatsApp, ждущие владельца (`link` карточки не бывает — чтение)."""
    found = []
    for op_id, hub_id in _pending_ids(ctx.conn, "group_op"):
        op = group_ops_mod.get(ctx.conn, op_id)
        if op is None:
            continue
        out = group_ops_mod.op_dict(op)
        people = ", ".join(p.get("name") or p.get("mask") or "?" for p in out["participants"])
        found.append(
            _card(
                ctx,
                ref=f"group_op:{op.id}",
                kind="group_op",
                project_id=op.project_id,
                hub_id=hub_id or ctx.hub_of_sub(op.author_sub_id),
                brief=_brief(_GROUP_OP_BRIEF.get(op.kind, op.kind), op.title, people),
                full=_mask_tree(out),
                created_ts=op.created_ts,
                role=out.get("role"),
                role_no=op.author_role_no,
                author_sub=op.author_sub_id,
            )
        )
    return found


def _rules(ctx: _Ctx, principal) -> list[dict]:
    found = []
    for request_id, hub_id in _pending_ids(ctx.conn, "rule_request"):
        req = rule_requests_mod.get(ctx.conn, request_id)
        if req is None:
            continue
        out = rule_requests_mod.request_dict(ctx.conn, req, full=rule_requests_mod.full_view(principal))
        found.append(
            _card(
                ctx,
                ref=f"rule:{req.id}",
                kind="rule",
                project_id=req.project_id,
                hub_id=hub_id,
                brief=_brief("правило раскладки", out.get("description")),
                full=_mask_tree(out),
                created_ts=req.created_ts,
            )
        )
    return found


def _agent_requests(ctx: _Ctx) -> list[dict]:
    found = []
    for req in agent_requests_mod._select(ctx.conn, "r.status = 'pending' ORDER BY r.id", ()):  # noqa: SLF001
        out = agent_requests_mod.request_dict(ctx.conn, req)
        payload = out.get("payload") or {}
        if req.kind == "move_channel":
            brief = _brief("перенести беседу", payload.get("channel_title"))
        else:
            brief = _brief("новая сессия", payload.get("role"))
        found.append(
            _card(
                ctx,
                ref=f"agent_request:{req.id}",
                kind="agent_request",
                project_id=req.project_id,
                hub_id=req.hub_session_id,
                brief=brief,
                full=_mask_tree(out),
                created_ts=req.created_ts,
            )
        )
    return found


def _reminder_auto(ctx: _Ctx, rec, hub_id: int | None) -> bool:
    """Ф66-1 (П5, ревью Р2, Р7): напоминание уходит «сразу да» (карточки владельцу нет), только если его сигнал
    сессии дошёл или есть живая подписка, которой он будет отдан, и маршрут той сессии — `auto` (ручная сессия
    хаба — владельцу); напоминание владельца из UI и роль без живой подписки — владельцу всегда. «Сразу да» по нему уже
    пробовали и не вышло (Р7) — владельцу. Ф89-1 (П1): повторяемое «сразу да» не исполняет никогда."""
    if rec.api_token_id is None or rec.every is not None:
        return False
    if rec.signaled_ts is not None:
        auto = ctx.route(rec.project_id, hub_id, "reminder") == card_route_mod.AUTO
    else:
        auto = any(
            ctx.route(rec.project_id, held_mod.held_id(sub.session_pid), "reminder") == card_route_mod.AUTO
            for sub in reminders_mod.listeners(ctx.conn, rec)
        )
    return auto and not card_route_mod.tried(ctx.conn, f"reminder:{rec.id}")


def _reminders(ctx: _Ctx, principal) -> list[dict]:
    """Сработавшие напоминания; сессия — та, чьей подписке ушёл сигнал срабатывания. Ф66-1 (П5, ревью Р2):
    «сразу да» (`_reminder_auto`) — карточки владельцу нет (закроет минутный цикл, когда сигнал сессии дойдёт).

    Ф89-1 (П1): повторяемое — карточка только когда сессия пропустила целый срок (`missed >= 1`), подвид
    `repeat` (ответы «Сделано» / «Снять»; при `auto` — агенту); без беседы — «напоминание <роль>»."""
    found = []
    for rec in reminders_mod._select(  # noqa: SLF001
        ctx.conn, "r.status = 'fired' AND (r.every IS NULL OR r.missed >= 1) ORDER BY r.fired_ts, r.id"
    ):
        hub_id = ctx.hub_of_sub(rec.signal_sub_id)
        if _reminder_auto(ctx, rec, hub_id):
            continue
        out = reminders_mod.reminder_dict(ctx.conn, rec, principal)
        title = (out.get("channel") or {}).get("title")
        if out["channel_id"] is None:
            title = f"напоминание {rec.role}" if rec.role else "напоминание без беседы"
        repeat = rec.every is not None
        missed = f"пропущено {rec.missed}" if repeat and rec.missed else None
        found.append(
            _card(
                ctx,
                ref=f"reminder:{rec.id}",
                kind="reminder",
                project_id=rec.project_id,
                hub_id=hub_id,
                brief=_brief("напоминание", title, out.get("every_text"), missed, out.get("note")),
                full=_mask_tree(out),
                created_ts=rec.fired_ts or rec.created_ts,
                role=rec.role,
                role_no=rec.role_no if rec.role is not None else None,
                sub=card_route_mod.REPEAT_SUB if repeat else None,
                author_sub=rec.signal_sub_id,
            )
        )
    return found


def _session_waits(ctx: _Ctx, now_ms: int, presence: list[dict]) -> list[dict]:
    """Сессии хаба, которые ждут владельца сами по себе: тяжёлая с ждущими сигналами (`heavy`), пауза лимита с
    репликами или сигналами (`paused`), прерванные реплики (`interrupted`, по карточке на реплику)."""
    conn = ctx.conn
    found = []
    for rec in held_mod.list_sessions(conn):
        if rec.wait_reason in ("heavy", "paused"):
            pending = held_signals_mod.pending_total(conn, rec, now_ms=now_ms, presence=presence)
            queued = held_work_mod.counts(conn, rec)[1]
            if rec.wait_reason == "heavy" and pending > 0:
                found.append(
                    _card(
                        ctx,
                        ref=f"heavy:{rec.id}",
                        kind="heavy",
                        project_id=rec.project_id,
                        hub_id=rec.id,
                        brief=_brief(f"{pending} сигналов ждут подъёма: контекст большой, кэш остыл"),
                        full={"pending": pending, "title": rec.title},
                        created_ts=rec.updated_ts,
                    )
                )
            elif rec.wait_reason == "paused" and (pending > 0 or queued > 0):
                card = _card(
                    ctx,
                    ref=f"paused:{rec.id}",
                    kind="paused",
                    project_id=rec.project_id,
                    hub_id=rec.id,
                    brief=_brief("пауза лимита подписки: реплика ждёт"),
                    full={"pending": pending, "queued_says": queued, "title": rec.title, "fallback": None},
                    created_ts=rec.updated_ts,
                )
                goal = engine_fallback_mod.offer(conn, rec, now_ms)
                if goal is not None:
                    # Ф83-1 (П6): «Перейти на <движок · модель>» рядом с «Отправить всё равно».
                    label = f"Перейти на {engine_fallback_mod.choice_label(goal)}"
                    card["answers"] = [*card["answers"], {**PAUSED_FALLBACK, "label": label}]
                    card["full"]["fallback"] = goal
                found.append(card)
        for say in held_work_mod.queued_says(conn, rec):
            if say["status"] != "interrupted":
                continue
            found.append(
                _card(
                    ctx,
                    ref=f"say:{say['id']}",
                    kind="interrupted",
                    project_id=rec.project_id,
                    hub_id=rec.id,
                    brief=_brief("прервана реплика", say["text"]),
                    full=_mask_tree(
                        {
                            "id": say["id"],
                            "text": say["text"],
                            "files": [f.get("name") for f in say["files"]],
                            "interrupted_ts": say["interrupted_ts"],
                        }
                    ),
                    created_ts=say["interrupted_ts"],
                )
            )
    return found


# --- карточка `turn` (Ф56-1б, раздел 3) ---------------------------------------------------------------------


def _payload(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _ui_say_in_turn(conn, rec: held_mod.HubSession) -> bool:
    """П6: за ход доставлена реплика владельца из интерфейса (`source='ui'`, доставлена не раньше начала хода,
    которое ядро ставит переходом в `running`). Начала хода нет (хост перезапускался посреди хода) — реплик
    не было. Ф60-1 (П1): подача расшифровки интервью (`interview_id`) — не реплика владельца, в счёт не идёт."""
    if rec.turn_ts is None or rec.store is None:
        return False
    row = conn.execute(
        held_work_mod._sql(  # noqa: SLF001 — та же подстановка схемы
            rec.store,
            "SELECT 1 FROM {schema}.held_say WHERE session_id = ? AND status = 'delivered' AND source = 'ui' "
            "AND interview_id IS NULL AND delivered_ts >= ? LIMIT 1",
        ),
        (rec.id, rec.turn_ts),
    ).fetchone()
    return row is not None


def close_turns(
    conn,
    hub_session_id: int,
    *,
    by: str,
    action: str,
    now_ms: int,
    say_id: int | None = None,
    token_id: int | None = None,
) -> int:
    """Закрыть открытые карточки `turn` сессии (их одна, 3.3): `by` — `ui` / `agent` / `system`, в `payload` —
    `action` (`reply`, `ok`, `superseded`, `closed`) и `say_id` реплики-ответа. Число закрытых."""
    rows = conn.execute(
        "SELECT id, payload_json, ref FROM main.owner_item WHERE kind = 'turn' AND session_id = ? "
        "AND closed_ts IS NULL ORDER BY id",
        (hub_session_id,),
    ).fetchall()
    for row_id, raw, _ref in rows:
        payload = {**_payload(raw), "action": action}
        if say_id is not None:
            payload["say_id"] = say_id
        conn.execute(
            "UPDATE main.owner_item SET closed_ts = ?, closed_by = ?, closed_token_id = ?, "
            "payload_json = ?, seq = ? WHERE id = ?",
            (now_ms, by, token_id, json.dumps(payload, ensure_ascii=False), next_seq(conn), row_id),
        )
    if action in ("reply", "ok") and by in ("ui", "agent"):
        # Ф76-1 (П5): ответ на ход — строка журнала решений (реплика владельца из окна сессии или ответ канала;
        # внутри ответа канала — от его имени, `decisions.acting`).
        text = decisions_mod.say_text(conn, say_id) if say_id is not None else None
        for _row_id, _raw, ref in rows:
            decisions_mod.record(
                conn, ref, decisions_mod.by_of_source(by), action, text, now_ms=now_ms, nested=True
            )
    return len(rows)


def turn_asks(full: str) -> bool:
    """Ответ сессии без «ВЛАДЕЛЬЦУ:» всё же чего-то ждёт от владельца (вопрос или просьба в двух последних абзацах)."""
    text = _TURN_URL.sub(" ", _TURN_FENCE.sub(" ", full or ""))
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not p.strip().startswith(">")]
    tail = paras[-2:]
    if any(_TURN_NOTHING.search(p) for p in tail):
        return False
    return any(_TURN_ASK.search(p) for p in tail)


def _turn_answer(answer) -> tuple[str, str, str, bool] | None:
    """`turn_result.answer` хоста -> (краткое, полное, вид краткого, обрезано); пустое или кривое — `None`."""
    if not isinstance(answer, dict):
        return None
    full, brief, kind = answer.get("full"), answer.get("brief"), answer.get("brief_kind")
    if not isinstance(full, str) or not full.strip() or kind not in TURN_BRIEF_KINDS:
        return None
    brief = brief if isinstance(brief, str) and brief.strip() else ""
    return brief, full, kind, answer.get("cut") is True


def open_turn_card(conn, rec: held_mod.HubSession, *, turns: int, answer, summary, now_ms: int) -> str | None:
    """3.3 (П5, П6, А7): конец хода сессии хаба `rec` (запись ДО конца хода — `turn_ts` = его начало) —
    завести карточку `turn`, если за ход доставлена реплика владельца из интерфейса (всегда) или ответ кончается
    строкой «ВЛАДЕЛЬЦУ:» (`brief_kind == "owner_channel"`). Прежняя открытая карточка сессии закрывается `superseded`.
    Тексты модели — чужой ввод: маскируются до записи. Возвращает `ref` заведённой карточки или `None`."""
    if rec.closed_ts is not None:
        return None
    by_ui = _ui_say_in_turn(conn, rec)
    parsed = _turn_answer(answer)
    if not by_ui and (parsed is None or parsed[2] != "owner_channel"):
        return None
    # Ф93-1 (A.3): денежный ход — по хвосту «ВЛАДЕЛЬЦУ:» (без него — по последнему абзацу); карточку даёт всегда.
    money = parsed is not None and card_route_mod.turn_money(parsed[1])
    # 23.10: «Ответ сессии» — «сразу да»: без «ВЛАДЕЛЬЦУ:» и без просьбы в конце ответа — карточки нет.
    if (
        (parsed is None or parsed[2] != "owner_channel")
        and not money
        and not (parsed is not None and turn_asks(parsed[1]))
        and card_route_mod.turn_silent(conn, rec.project_id, rec.id)
    ):
        return None
    if parsed is None:
        brief, full, kind, cut = TURN_NO_ANSWER, summary or TURN_NO_ANSWER, None, False
    else:
        brief, full, kind, cut = parsed
        if not brief:
            brief = TURN_NO_ANSWER
    full = _mask(full)
    if len(full) > TURN_FULL_MAX:
        full, cut = full[: TURN_FULL_MAX - 1] + "…", True
    payload = {
        "brief": _clip_text(_mask(brief).strip(), TURN_BRIEF_MAX),
        "full": full,
        "brief_kind": kind,
        "cut": cut,
        "turns": turns,
        "by_ui_say": by_ui,
        "money": money,
    }
    old_refs = [
        r[0]
        for r in conn.execute(
            "SELECT ref FROM main.owner_item WHERE kind = 'turn' AND session_id = ? AND closed_ts IS NULL",
            (rec.id,),
        ).fetchall()
    ]
    close_turns(conn, rec.id, by="system", action="superseded", now_ms=now_ms)
    ref = f"turn:{rec.id}:{turns}"
    conn.execute(
        "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, payload_json, seq) "
        "VALUES (?, 'turn', ?, ?, ?, ?, ?)",
        (ref, rec.project_id, rec.id, now_ms, json.dumps(payload, ensure_ascii=False), next_seq(conn)),
    )
    # Ревью Ф93-1 (Б2): денежная карточка при «Деньги» = «Мне» поручение не принимает — ни взятое агентом
    # самим, ни «Реши» владельца по прежней карточке: оно закрывается, как при вытеснении без переезда.
    _carry_errands(conn, old_refs, ref, close=money and _money_mine(conn, rec.project_id), now_ms=now_ms)
    return ref


def _money_mine(conn, project_id: int | None) -> bool:
    """Ф93-1: строка «Деньги» таблицы проекта — «Мне»."""
    return card_route_mod.money_route(card_route_mod.table(conn, project_id)) == card_route_mod.ME


def _carry_errands(conn, old_refs: list[str], new_ref: str, *, close: bool = False, now_ms: int = 0) -> None:
    """Живая жалоба владельца: «Реши» по карточке `turn`, а сессия тем временем закончила ещё ход (сигнал
    агента) — новая карточка вытеснила старую, и ядро закрыло поручение как «карточку закрыли»; агент
    бросал работу. Поручение по вытесненной карточке переезжает на новую карточку той же сессии (у неё «у
    агента»), прежние цели — в `prev_targets`: агент отвечает по старому `ref` (принимается репликой сессии,
    `_answer_turn`), и его ответ закрывает переехавшее поручение (`_close_own_errand`). Ревью Ф93-1 (Б2):
    `close` — новая карточка денежная при «Деньги» = «Мне»: поручение не переезжает, а закрывается `system`
    без причины возврата (боту «вернул тебе» не идёт — денежную карточку владелец и так получает)."""
    if not old_refs:
        return
    errands = _open_errands(conn)
    for old in old_refs:
        errand = errands.get(old)
        if errand is None:
            continue
        if close:
            _close_errand(conn, errand, "system", now_ms=now_ms)
            continue
        prev = [r for r in errand["_payload"].get("prev_targets") or [] if isinstance(r, str)]
        payload = {**errand["_payload"], "target": new_ref, "prev_targets": [*prev, old]}
        conn.execute(
            "UPDATE main.owner_item SET payload_json = ?, seq = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), next_seq(conn), errand["id"]),
        )


def _turns(ctx: _Ctx) -> list[dict]:
    """Открытые карточки `turn` незакрытых сессий хаба (закрытие сессии их закрывает само)."""
    found = []
    rows = ctx.conn.execute(
        "SELECT ref, project_id, session_id, created_ts, payload_json FROM main.owner_item "
        "WHERE kind = 'turn' AND closed_ts IS NULL ORDER BY id"
    ).fetchall()
    for ref, project_id, hub_id, created_ts, raw in rows:
        rec = ctx.session(hub_id)
        if rec is None or rec.closed_ts is not None:
            continue
        payload = _payload(raw)
        found.append(
            _card(
                ctx,
                ref=ref,
                kind="turn",
                project_id=project_id,
                hub_id=hub_id,
                brief=str(payload.get("brief") or TURN_NO_ANSWER),
                full={
                    "answer": payload.get("full"),
                    "cut": bool(payload.get("cut")),
                    "turns": payload.get("turns"),
                    "by_ui_say": bool(payload.get("by_ui_say")),
                    "state": rec.state,
                    "wait_reason": rec.wait_reason,
                },
                created_ts=created_ts,
                money=card_route_mod.is_money("turn", payload),
            )
        )
    return found


def _turn_row(conn, ref: str) -> tuple[int, int | None, int | None, int | None] | None:
    """Строка карточки `turn` по `ref`: (id, проект, сессия хаба, закрыта когда)."""
    return conn.execute(
        "SELECT id, project_id, session_id, closed_ts FROM main.owner_item WHERE ref = ? AND kind = 'turn' "
        "ORDER BY id DESC LIMIT 1",
        (ref,),
    ).fetchone()


# --- состояния сессии хаба (Ф56-1в: `stall`, `broken`, `dead`, `stuck`; П12, А1–А3, А8) -------------------


def _state_row(conn, ref: str, kind: str) -> tuple[int, int | None, int | None, int | None] | None:
    """Строка карточки состояния по `ref`: (id, проект, сессия хаба, закрыта когда)."""
    return conn.execute(
        "SELECT id, project_id, session_id, closed_ts FROM main.owner_item WHERE ref = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (ref, kind),
    ).fetchone()


def _close_row(
    conn,
    row_id: int,
    raw,
    *,
    by: str,
    action: str,
    now_ms: int,
    say_id: int | None = None,
    token_id: int | None = None,
) -> bool:
    """Закрыть одну открытую строку `owner_item` (`action`, `say_id` — в `payload`); уже закрытая —
    `False`."""
    payload = {**_payload(raw), "action": action}
    if say_id is not None:
        payload["say_id"] = say_id
    done = conn.execute(
        "UPDATE main.owner_item SET closed_ts = ?, closed_by = ?, closed_token_id = ?, payload_json = ?, "
        "seq = ? WHERE id = ? AND closed_ts IS NULL",
        (now_ms, by, token_id, json.dumps(payload, ensure_ascii=False), next_seq(conn), row_id),
    ).rowcount
    return bool(done)


def close_states(
    conn,
    hub_session_id: int,
    kinds,
    *,
    by: str,
    action: str,
    now_ms: int,
    keep_hold: bool = False,
    say_id: int | None = None,
    token_id: int | None = None,
) -> int:
    """Закрыть открытые карточки состояний `kinds` сессии. `keep_hold` — строки «Пусть ждёт» (`hold_ts`)
    остаются (их снимает только реплика, 2.2). Число закрытых."""
    kinds = tuple(kinds)
    marks = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"SELECT id, payload_json FROM main.owner_item WHERE session_id = ? AND kind IN ({marks}) "  # noqa: S608
        f"AND closed_ts IS NULL{' AND hold_ts IS NULL' if keep_hold else ''} ORDER BY id",
        (hub_session_id, *kinds),
    ).fetchall()
    closed = 0
    for row_id, raw in rows:
        closed += _close_row(
            conn, row_id, raw, by=by, action=action, now_ms=now_ms, say_id=say_id, token_id=token_id
        )
    return closed


def close_stall_on_say(conn, hub_session_id: int, *, by: str, say_id: int, now_ms: int, token_id=None) -> int:
    """2.2: реплика сессии (из окна или по карточке) закрывает её «стоит без дела»: строку «Пусть ждёт» —
    `action='said'` (hold снят), открытую карточку — `action='reply'`; `closed_by` — источник реплики."""
    rows = conn.execute(
        "SELECT id, payload_json, hold_ts FROM main.owner_item WHERE session_id = ? AND kind = 'stall' "
        "AND closed_ts IS NULL ORDER BY id",
        (hub_session_id,),
    ).fetchall()
    for row_id, raw, hold_ts in rows:
        action = "said" if hold_ts is not None else "reply"
        _close_row(conn, row_id, raw, by=by, action=action, now_ms=now_ms, say_id=say_id, token_id=token_id)
    return len(rows)


def _open_rows(conn, hub_session_id: int, kind: str) -> list[tuple[int, str, int | None]]:
    return conn.execute(
        "SELECT id, ref, hold_ts FROM main.owner_item WHERE session_id = ? AND kind = ? "
        "AND closed_ts IS NULL ORDER BY id",
        (hub_session_id, kind),
    ).fetchall()


def _open_state(conn, rec: held_mod.HubSession, kind: str, number: int, payload: dict, now_ms: int) -> str:
    """Новая карточка состояния: прежняя открытая того же вида у сессии закрывается `superseded` (одна
    открытая каждого вида на сессию). Тексты в `payload` — уже маскированные."""
    close_states(conn, rec.id, (kind,), by="system", action="superseded", now_ms=now_ms)
    ref = f"{kind}:{rec.id}:{number}"
    conn.execute(
        "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, payload_json, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ref, kind, rec.project_id, rec.id, now_ms, json.dumps(payload, ensure_ascii=False), next_seq(conn)),
    )
    return ref


def open_broken(conn, rec: held_mod.HubSession, *, note: str, expired: int, now_ms: int) -> str | None:
    """3 (А1): ход оборван усыплением извне (рестарт хоста, хост замолчал, обрыв хабом). `rec` — запись ДО
    усыпления (состояние, начало хода и шаг ещё её). Зовётся в той же транзакции, что усыпление."""
    if rec.closed_ts is not None:
        return None
    from . import agent_state  # импорт внутри: модуль состояния роли читает ход сессии

    step = agent_state._step(conn, rec)  # noqa: SLF001 — тот же шаг, что в состоянии роли
    payload = _mask_tree(
        {
            "note": note,
            "was_running": rec.state == "running",
            "expired": expired,
            "origin": rec.turn_origin,
            "since_ts": rec.turn_ts,
            "step": step,
        }
    )
    return _open_state(conn, rec, "broken", rec.turns, payload, now_ms)


def open_dead(conn, rec: held_mod.HubSession, *, failure: str, fails: int, now_ms: int) -> str | None:
    """4 (А2): третья неудача запуска подряд — хост больше не пробует. Открытая `dead` уже есть — ничего."""
    if rec.closed_ts is not None or _open_rows(conn, rec.id, "dead"):
        return None
    payload = {
        "failure": _clip_text(_mask(failure), BRIEF_MAX),
        "fails": fails,
        "engine": rec.engine,
    }
    return _open_state(conn, rec, "dead", rec.turns, payload, now_ms)


def stuck_report(conn, rec: held_mod.HubSession, stuck, *, now_ms: int) -> str | None:
    """5.2 (А3): хост доложил о зависшем ходе (`stuck` — словарь) — карточка `stuck:<сессия>:<turns + 1>`
    (идущий ход): нет — завести, есть — обновить; `None` — шаг пришёл, открытая закрывается `stepped`."""
    if stuck is None:
        close_states(conn, rec.id, ("stuck",), by="system", action="stepped", now_ms=now_ms)
        return None
    if rec.closed_ts is not None or not isinstance(stuck, dict):
        return None
    step = stuck.get("step") if isinstance(stuck.get("step"), dict) else None
    tool = step.get("tool") if step is not None else None
    payload = _mask_tree(
        {
            "since_ts": stuck.get("since_ts"),
            "step": {"tool": tool if isinstance(tool, str) and tool else None},
            "origin": rec.turn_origin,
            "turn_since_ts": rec.turn_ts,
        }
    )
    ref = f"stuck:{rec.id}:{rec.turns + 1}"
    for row_id, row_ref, _hold in _open_rows(conn, rec.id, "stuck"):
        if row_ref == ref:
            conn.execute(
                "UPDATE main.owner_item SET payload_json = ?, seq = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), next_seq(conn), row_id),
            )
            return ref
    return _open_state(conn, rec, "stuck", rec.turns + 1, payload, now_ms)


def on_turn(
    conn,
    before: held_mod.HubSession,
    after: held_mod.HubSession,
    changes: dict,
    *,
    ended: bool,
    failure: str | None,
    now_ms: int,
) -> None:
    """`held_work.turn` (та же транзакция, после записи): карточки состояний по сообщению хоста.
    - ход пошёл (`running`) — «стоит без дела» и «ход оборван» закрываются `worked` («Пусть ждёт» остаётся);
      ключ `stuck` — доклад хоста о зависании (5.2);
    - не `running` — открытая `stuck` закрывается: `turn_end` (конец хода) или `asleep` (сон);
    - третья неудача запуска подряд — `dead`; запуск удался (ход пошёл, либо не `asleep` без неудачи при
      бывшем счётчике) — открытая `dead` закрывается `launched`;
    - идущий ход оборван хабом (`cut` с `asleep`) — `broken`."""
    state = changes.get("state")
    if state == "running":
        close_states(
            conn, after.id, ("stall", "broken"), by="system", action="worked", now_ms=now_ms, keep_hold=True
        )
        # Ревью Ф56 (1): ход пошёл — сессия запускается, как бы счётчик неудач ни обнулили (реплика из окна
        # обнуляет его раньше запуска, и ветка «счётчик был» ниже карточку не закрыла бы).
        close_states(conn, after.id, ("dead",), by="system", action="launched", now_ms=now_ms)
        if "stuck" in changes:
            stuck_report(conn, after, changes["stuck"], now_ms=now_ms)
    else:
        action = "asleep" if state == "asleep" and not ended else "turn_end"
        close_states(conn, after.id, ("stuck",), by="system", action=action, now_ms=now_ms)
    if failure is not None:
        if after.launch_fails >= LAUNCH_FAILS_DEAD:
            open_dead(conn, after, failure=after.failure or failure, fails=after.launch_fails, now_ms=now_ms)
    elif state != "asleep" and before.launch_fails > 0:
        close_states(conn, after.id, ("dead",), by="system", action="launched", now_ms=now_ms)
    if changes.get("cut") and state == "asleep" and before.state == "running":
        open_broken(conn, before, note=NOTE_CUT, expired=0, now_ms=now_ms)


# «Стоит без дела» (Ф56-1в) отменена (владелец 22.10): сессии будят сообщения, сигналы и напоминания, а карточка
# простоя только лишний раз дёргала модель агента. Старые строки `stall` закрываются, новых нет.


def retire_stalls(conn, *, now_ms: int) -> list[str]:
    """Минутный цикл ядра: открытые карточки «стоит без дела» (и спрятанные «Пусть ждёт») — закрыть
    системой (`retired`). Возвращает `ref` закрытых."""
    rows = conn.execute(
        "SELECT ref, session_id FROM main.owner_item WHERE kind = 'stall' AND closed_ts IS NULL ORDER BY id"
    ).fetchall()
    for _ref, session_id in rows:
        close_states(conn, session_id, ("stall",), by="system", action="retired", now_ms=now_ms)
    return [ref for ref, _sid in rows]


def hold(conn, row_id: int, *, now_ms: int) -> None:
    """2.2: «Пусть ждёт» — строка `stall` не закрывается, а прячется (`hold_ts`): в очереди её нет ни владельцу, ни
    агенту, новых `stall` сессии минутный цикл не заводит, ход её не закрывает; снимает только реплика."""
    row = conn.execute("SELECT closed_ts, hold_ts FROM main.owner_item WHERE id = ?", (row_id,)).fetchone()
    if row is None or row[0] is not None:
        raise OwnerError(409, STATE_CLOSED)
    if row[1] is not None:
        raise OwnerError(409, "сессия уже ждёт («Пусть ждёт»)")
    conn.execute(
        "UPDATE main.owner_item SET hold_ts = ?, seq = ? WHERE id = ?", (now_ms, next_seq(conn), row_id)
    )


def retry_launch(conn, hub_session_id: int, *, now_ms: int, presence: list[dict], audit: dict) -> None:
    """4 (А2): «Попробовать снова» — счётчик неудач запуска и причина обнуляются, сессия поднимается
    (`held_signals.wake`: спящую без ждущего — под постоянную строку, иначе хосту нечего запускать)."""
    rec = held_work_mod._open_ui(conn, hub_session_id)  # noqa: SLF001
    if rec.launch_fails or rec.failure is not None:
        hub_sessions_mod.touch(conn, rec.id, now_ms, launch_fails=0, failure=None)
    held_signals_mod.wake(conn, rec.id, now_ms=now_ms, presence=presence, audit=audit)


def _minutes_since(ts, now_ms: int) -> int:
    return max(0, (now_ms - ts) // 60_000) if isinstance(ts, int) and not isinstance(ts, bool) else 0


def _state_card(kind: str, rec: held_mod.HubSession, payload: dict, now_ms: int) -> tuple[str, dict]:
    """(краткое, полное) карточки состояния по виду (тексты `payload` маскированы при записи)."""
    if kind == "stall":
        minutes = _minutes_since(payload.get("silent_since"), now_ms)
        brief = f"стоит без дела {minutes} мин" + (", спит" if rec.state == "asleep" else "")
        full = {
            "silent_min": minutes,
            "state": rec.state,
            "subs": payload.get("subs") or [],
            "reminders": payload.get("reminders") or [],
            "bg": payload.get("bg"),
            "wake_at_ts": payload.get("wake_at_ts"),
        }
        return _brief(brief), full
    if kind == "broken":
        step = payload.get("step") if isinstance(payload.get("step"), dict) else None
        text = f"ход оборван: {payload.get('note') or ''}"
        if step is not None and step.get("tool"):
            text += f", занималась: {step['tool']}"
        return _brief(text), {**payload, "state": rec.state}
    if kind == "dead":
        return _brief(f"не запускается ({payload.get('fails')} раз): {payload.get('failure') or ''}"), payload
    # stuck
    step = payload.get("step") if isinstance(payload.get("step"), dict) else {}
    minutes = _minutes_since(payload.get("since_ts"), now_ms)
    tool = step.get("tool")
    text = f"завис: шаг {tool} не возвращается {minutes} мин" if tool else f"завис на {minutes} мин"
    return _brief(text), {**payload, "state": rec.state}


def _states(ctx: _Ctx, now_ms: int) -> list[dict]:
    """Открытые карточки состояний незакрытых сессий хаба; «Пусть ждёт» (`hold_ts`) не отдаётся."""
    found = []
    rows = ctx.conn.execute(
        "SELECT ref, kind, project_id, session_id, created_ts, payload_json FROM main.owner_item "
        "WHERE kind IN ('stall', 'broken', 'dead', 'stuck') AND closed_ts IS NULL AND hold_ts IS NULL "
        "ORDER BY id"
    ).fetchall()
    for ref, kind, project_id, hub_id, created_ts, raw in rows:
        rec = ctx.session(hub_id)
        if rec is None or rec.closed_ts is not None:
            continue
        brief, full = _state_card(kind, rec, _payload(raw), now_ms)
        found.append(
            _card(
                ctx,
                ref=ref,
                kind=kind,
                project_id=project_id,
                hub_id=hub_id,
                brief=brief,
                full=full,
                created_ts=created_ts,
            )
        )
    return found


def _answer_state(
    conn,
    loc: _Loc,
    action: str,
    *,
    text,
    source: str,
    principal,
    now_ms: int,
    audit: dict,
    presence,
    files=None,
) -> tuple[str, None]:
    """Ответ по карточке состояния (разделы 2–5 спеки). Карточка уже закрыта — 409. Строка закрывается с
    `closed_by` = источник ответа (`token_id` агента), кроме «Пусть ждёт» (строка прячется)."""
    row = conn.execute("SELECT closed_ts FROM main.owner_item WHERE id = ?", (loc.number,)).fetchone()
    if row is None or row[0] is not None:
        raise OwnerError(409, STATE_CLOSED)
    token_id = principal.token_id if source == "agent" else None
    sid = loc.hub_id

    def close(name: str, say_id: int | None = None) -> None:
        current = conn.execute(
            "SELECT payload_json FROM main.owner_item WHERE id = ?", (loc.number,)
        ).fetchone()
        _close_row(
            conn,
            loc.number,
            current[0] if current is not None else None,
            by=source,
            action=name,
            now_ms=now_ms,
            say_id=say_id,
            token_id=token_id,
        )

    if action in ("say", "interrupt"):
        say_id = held_work_mod.say(
            conn,
            sid,
            text=text or "",
            files=files,
            priority="now" if action == "interrupt" else "next",
            now_ms=now_ms,
            audit=audit,
            source=source,
            token_id=token_id,
            typed=source == "ui",  # Ф86-1 (П4): ответ кукой — набрал владелец; агент владельца — нет
        )
        close("interrupt" if action == "interrupt" else "reply", say_id)  # `stall` реплика уже закрыла
        return "queued", None
    if action == "hold":
        hold(conn, loc.number, now_ms=now_ms)
        return "held", None
    if action == "wake":
        rec = held_mod.get_open(conn, sid)
        if rec is not None and rec.state != "asleep":
            # Ревью Ф56 (6): сессию уже подняли (из окна, по реплике) — поднимать нечего, карточка закрыта.
            close("wake")
            return "closed", None
        held_signals_mod.wake(conn, sid, now_ms=now_ms, presence=presence, audit=audit)
        close("wake")
        return "wake_requested", None
    if action == "retry":
        retry_launch(conn, sid, now_ms=now_ms, presence=presence, audit=audit)
        close("retry")
        return "wake_requested", None
    if action == "stop":
        held_work_mod.sleep(conn, sid, now_ms=now_ms, audit=audit)
        close("stop")
        return "sleep_requested", None
    close(action)  # `sleep` («Пусть спит»), `wait` («Пусть работает»)
    return "closed", None


# --- сообщения агентов владельцу (Ф56-1б, раздел 4; А4) ---------------------------------------------------------


AGENT_MSG_FROM_META = "owner_agent_msg_from_id"


def agent_msg_from_id(conn) -> int:
    """Первый номер `peer_msg`, с которого сообщения агентов владельцу идут в очередь (`0084_main.sql`)."""
    raw = engine_mod._meta_get(conn, AGENT_MSG_FROM_META)  # noqa: SLF001
    try:
        return int(raw) if raw is not None else 0
    except ValueError:
        return 0


def agent_msg_in_channel(conn, rec: peer_msgs_mod.Rec, hub_id: int | None) -> bool:
    """Сообщение агента владельцу «у агента» — по таблице маршрута проекта автора (Ф66-1, B.1); агент вне
    проектов (проект 0) — всегда владельцу (П7)."""
    return in_channel(
        conn, rec.from_project_id, hub_id, "agent_msg", ref=f"agent_msg:{rec.id}", money=bool(rec.money)
    )


def _agent_msgs(ctx: _Ctx) -> list[dict]:
    """Ждущие сообщения агентов владельцу (`to_user`): ответы на его сообщения и вопросы `hub_ask_owner`."""
    found = []
    # Ревью Ф56 (9): сообщения, ждавшие владельца до появления очереди, карточками не становятся — порог по
    # номеру строки записан миграцией `0084_main.sql` (`meta.owner_agent_msg_from_id`); нет записи — все.
    since_id = agent_msg_from_id(ctx.conn)
    rows = peer_msgs_mod._select(  # noqa: SLF001 — тот же отсев старых строк, что у выдач модуля
        ctx.conn, "m.status = 'pending' AND m.to_user = 1 AND m.id >= ? ORDER BY m.id", (since_id,)
    )
    for rec in rows:
        hub_id = ctx.hub_of_sub(rec.from_sub_id)
        brief = _brief(rec.text)
        brief = brief if len(brief) <= AGENT_BRIEF_MAX else brief[: AGENT_BRIEF_MAX - 1] + "…"
        card = _card(
            ctx,
            ref=f"agent_msg:{rec.id}",
            kind="agent_msg",
            project_id=rec.from_project_id,
            hub_id=hub_id,
            brief=brief,
            full=_mask_tree(
                {
                    "text": rec.text,
                    "ask": bool(rec.ask),
                    "about_role": rec.ask_about,
                    "reply_to": rec.reply_to,
                    "chain_id": rec.chain_id,
                    "money": bool(rec.money),  # Ф93-1 (A.4)
                }
            ),
            created_ts=rec.created_ts,
            until_ts=rec.expires_ts,
            author_sub=rec.from_sub_id,
            money=card_route_mod.is_money("agent_msg", rec),
        )
        # Автор — линия сообщения (как её записало ядро при отправке), а не нынешняя роль сессии хаба.
        card["role"], card["role_no"] = rec.from_role, rec.from_no
        card["path"]["author"] = rec.from_role
        if rec.ask and rec.ask_about:
            card["path"]["about"] = _mask(rec.ask_about)
        found.append(card)
    return found


def _collect(conn, principal, *, now_ms: int, presence: list[dict]) -> list[dict]:
    """Все открытые карточки, видимые принципалу по проекту (без отбора «в канале»), в порядке очереди."""
    ctx = _Ctx(conn)
    cards = [
        *_held_requests(ctx, now_ms),
        *_humans(ctx, now_ms),
        *_drafts(ctx, principal, now_ms),
        *_presses(ctx),
        *_joins(ctx),
        *_leaves(ctx),
        *_group_ops(ctx),
        *_rules(ctx, principal),
        *_agent_requests(ctx),
        *_reminders(ctx, principal),
        *_session_waits(ctx, now_ms, presence),
        *_turns(ctx),
        *_states(ctx, now_ms),
        *_agent_msgs(ctx),
    ]
    cards = [c for c in cards if _visible(principal, c["project_id"])]
    cards.sort(key=lambda c: (_ORDER[c["kind"]], c["created_ts"] or 0, c["ref"]))
    return cards


def is_agent(principal) -> bool:
    """Агент владельца — bearer ровно со скоупом `human` (не бот `human_bot`, не сессия)."""
    return not principal.is_ui and principal.scopes == HUMAN_SCOPES


# Ф61-1 (B1): короткое название карточки для вопроса агента в боте — видимость как у владельца (все проекты).
_TITLE_VIEWER = Principal(token_id=0, kind="ui", scopes=frozenset(), project_ids=None)
TITLE_MAX = 200


def card_titles(conn, refs, *, now_ms: int) -> dict[str, str]:
    """`{ref: название}` открытых карточек очереди из `refs`: «проект · что нужно» (`brief`; пусто — путь).
    Закрытой или чужой строки в ответе нет."""
    wanted = {r for r in refs if isinstance(r, str)}
    if not wanted:
        return {}
    out: dict[str, str] = {}
    for card in _collect(conn, _TITLE_VIEWER, now_ms=now_ms, presence=[]):
        if card["ref"] not in wanted:
            continue
        path = card.get("path") or {}
        brief = card.get("brief") or " · ".join(str(v) for v in path.values() if v)
        title = " · ".join(p for p in (card.get("project_name"), brief) if p) or card["ref"]
        out[card["ref"]] = title if len(title) <= TITLE_MAX else title[: TITLE_MAX - 1] + "…"
    return out


def items(
    conn,
    *,
    principal,
    now_ms: int,
    waiting=None,
    presence: list[dict] | None = None,
    everything: bool = False,
) -> list[dict]:
    """Очередь канала владельца принципалу: кука — все открытые карточки (и `in_channel=false`); агент владельца —
    карточки `in_channel=true` проектов токена, карточки с открытым поручением «Реши» (П2 Ф66-1) и свои
    поручения (карточки `errand`, у них всегда `in_channel=true`). `waiting` — запас под следующие куски
    (живость ожиданий подписок). Ф65-1: у карточки `auto_ok` (агент берёт её сам — Ф66-1: маршрут `agent`) и
    `agent_ask` (открытый вопрос агента по ней, Ж5).

    Ф75-1: `everything` — агенту вся открытая очередь владельца (и карточки маршрута «Мне», `in_channel=false`):
    для разговора «Мой агент», где агент действует по слову владельца (`owner_word`). Проекты с закрытым днём
    агенту не отдаются и так; `auto_ok` и кнопки агента — по-прежнему только «в канале»."""
    from . import owner_talk as talk_mod  # импорт внутри: разговор сам опирается на этот модуль

    del waiting
    cards = _collect(conn, principal, now_ms=now_ms, presence=presence or [])
    agent = not principal.is_ui
    errands = _open_errands(conn)
    if agent:
        # Ф64-1 (раздел 3): карточки проектов с закрытым днём агенту владельца не отдаются (кука видит всё).
        closed = workday_mod.closed_projects(conn)
        cards = [
            c
            for c in cards
            if (everything or c["in_channel"] or c["ref"] in errands) and c["project_id"] not in closed
        ]
    state = agent_state(conn, now_ms)
    returned = _returned_errands(conn)
    asks = talk_mod.open_asks(conn, now_ms=now_ms) if cards else {}
    for card in cards:
        errand = errands.get(card["ref"])
        card["errand"] = _errand_public(errand) if errand is not None else None
        card["errand_returned"] = returned.get(card["ref"])
        card["auto_ok"] = card["in_channel"]
        card["agent_ask"] = asks.get(card["ref"])
        if state["online"] and card["in_channel"]:
            card["agent_actions"] = [
                {"id": a["id"], "label": a["label"], "needs_text": a["needs_text"]}
                for a in state["actions"]
                if not a["kinds"] or card["kind"] in a["kinds"]
            ]
    if agent:
        by_ref = {c["ref"]: c for c in cards}
        cards = cards + [
            _errand_card(e, by_ref[e["target"]]) for e in errands.values() if e["target"] in by_ref
        ]
    return cards


# --- общий ответ (раздел 4, П8, П9) -------------------------------------------------------------------------


class _Loc(NamedTuple):
    """Где карточка: вид, номер, проект, сессия хаба, подвид (`permission`/`question` у запросов задач)."""

    kind: str
    number: int
    project_id: int | None
    hub_id: int | None
    sub: str | None = None


def parse_ref(ref: str) -> tuple[str, int]:
    """`<приставка>:<номер>` -> (приставка, номер); чужое — 404. У `turn:<сессия>:<ход>` (и у состояний
    `stall`/`broken`/`dead`/`stuck`, Ф56-1в) номер — сессия (саму карточку ищет `_locate` по `ref`
    целиком)."""
    m = _TURN_REF_RE.match(ref or "")
    if m is not None:
        if not (1 <= int(m.group(2)) <= _ID_MAX and int(m.group(3)) <= _ID_MAX):
            raise OwnerError(404, "not found")
        return m.group(1), int(m.group(2))
    m = _REF_RE.match(ref or "")
    if m is None or m.group(1) not in _PREFIX or not 1 <= int(m.group(2)) <= _ID_MAX:
        raise OwnerError(404, "not found")
    return m.group(1), int(m.group(2))


def _hub_of_sub(conn, sub_id: int | None) -> int | None:
    if sub_id is None:
        return None
    sub = subs_mod.get_sub(conn, sub_id)
    return held_mod.held_id(sub.session_pid) if sub is not None else None


def _press_hub(press, by_sub: int | None) -> int | None:
    """Ревью Р1: сессия хаба — автор нажатия: по подписке автора, а без неё — по условной тройке, которую
    строка хранит с момента нажатия (`session_json`)."""
    if by_sub is not None:
        return by_sub
    return card_route_mod.hub_of_triple(presses_mod._session(press))  # noqa: SLF001


def _hub_column(conn, table: str, number: int) -> int | None:
    row = conn.execute(f"SELECT hub_session_id FROM main.{table} WHERE id = ?", (number,)).fetchone()  # noqa: S608
    return row[0] if row is not None else None


def _say_session(conn, say_id: int) -> held_mod.HubSession | None:
    schema = db_of(say_id)
    row = conn.execute(
        f"SELECT session_id FROM {schema}.held_say WHERE id = ?",  # noqa: S608 — схема из `db_of`
        (say_id,),
    ).fetchone()
    if row is None:
        return None
    rec = held_mod.get(conn, row[0])
    return rec if rec is not None and rec.store == schema else None


def _locate(conn, prefix: str, number: int, now_ms: int, ref: str = "") -> _Loc | None:
    """Карточка по `ref` — и решённая тоже (решённую отвергнет функция ядра 409); нет такой — `None`. У `turn`
    номер в `_Loc` — строка `owner_item`."""
    kind = _PREFIX[prefix]
    if prefix == "turn":
        row = _turn_row(conn, ref)
        return None if row is None else _Loc(kind, row[0], row[1], row[2])
    if prefix in STATE_KINDS:
        row = _state_row(conn, ref, prefix)
        return None if row is None else _Loc(kind, row[0], row[1], row[2])
    if prefix == "agent_msg":
        msg = peer_msgs_mod.get(conn, number)
        if msg is None or not msg.to_user:
            return None
        return _Loc(kind, number, msg.from_project_id, _hub_of_sub(conn, msg.from_sub_id))
    if prefix == "req":
        try:
            request, rec = held_work_mod._request(conn, number)  # noqa: SLF001
        except SessionError:
            return None
        return _Loc(request["kind"], number, rec.project_id, rec.id)
    if prefix == "human":
        req = human_mod.get(conn, number)
        if req is None:
            return None
        return _Loc(kind, number, req.project_id, _hub_of_sub(conn, req.author_sub_id))
    if prefix == "draft":
        schema = db_of(number)
        row = conn.execute(
            f"SELECT c.project_id, o.author_sub_id, o.created_ts FROM {schema}.outbox o "  # noqa: S608
            f"JOIN {schema}.channel c ON c.id = o.channel_id WHERE o.id = ?",
            (number,),
        ).fetchone()
        if row is None:
            return None
        hub_id = _hub_of_sub(conn, row[1])
        if hub_id is None:
            hub_id = card_route_mod.draft_hub(conn, number, row[2])  # ревью Р1: сессия хаба без подписки
        return _Loc(kind, number, row[0], hub_id)
    if prefix == "press":
        press = presses_mod.get(conn, number)
        return (
            None
            if press is None
            else _Loc(
                kind, number, press.project_id, _press_hub(press, _hub_of_sub(conn, press.author_sub_id))
            )
        )
    if prefix in ("join", "leave", "rule", "group_op"):
        module = {
            "join": join_mod,
            "leave": leave_mod,
            "rule": rule_requests_mod,
            "group_op": group_ops_mod,
        }[prefix]
        req = module.get(conn, number)
        if req is None:
            return None
        table = {
            "join": "join_request",
            "leave": "leave_request",
            "rule": "rule_request",
            "group_op": "group_op",
        }[prefix]
        return _Loc(kind, number, req.project_id, _hub_column(conn, table, number))
    if prefix == "agent_request":
        req = agent_requests_mod.get(conn, number)
        return None if req is None else _Loc(kind, number, req.project_id, req.hub_session_id)
    if prefix == "reminder":
        rec = reminders_mod.get(conn, number)
        if rec is None:
            return None
        sub = card_route_mod.REPEAT_SUB if rec.every is not None else None  # Ф89-1 (П1)
        return _Loc(kind, number, rec.project_id, _hub_of_sub(conn, rec.signal_sub_id), sub)
    if prefix in ("heavy", "paused"):
        rec = held_mod.get(conn, number)
        return None if rec is None else _Loc(kind, number, rec.project_id, rec.id)
    rec = _say_session(conn, number)  # say:
    return None if rec is None else _Loc(kind, number, rec.project_id, rec.id)


def _check_body(
    spec: dict, text: str | None, answers: dict | None, send_now: bool, kind: str, *, has_files: bool = False
) -> None:
    action = spec["action"]
    if spec["needs_text"] and not text and not has_files:
        raise OwnerError(422, f"text: у «{action}» нужен текст")
    if text is not None and not spec["takes_text"]:
        raise OwnerError(422, f"text: у «{action}» текста нет")
    limit = _TEXT_LIMIT.get(kind, TEXT_MAX)
    if text is not None and limit is not None and len(text) > limit:
        raise OwnerError(422, f"text: не длиннее {limit}")
    if spec["needs_answers"] and not answers:
        raise OwnerError(422, "answers: ответы на вопросы {вопрос: ответ}")
    if answers is not None and not spec["needs_answers"]:
        raise OwnerError(422, f"answers: у «{action}» ответов на вопросы нет")
    if send_now and not (kind == "draft" and action == "approve"):
        raise OwnerError(422, "send_now — только у подтверждения черновика")


def _takes_files(kind: str, action: str) -> bool:
    """Ответ с файлами — только реплика сессии хаба: `turn` -> `reply`, состояние -> `say`/`interrupt`."""
    return (kind == "turn" and action == "reply") or (kind in STATE_KINDS and action in ("say", "interrupt"))


def _still_open(conn, principal, ref: str, *, now_ms: int, presence: list[dict]) -> bool:
    return any(c["ref"] == ref for c in _collect(conn, principal, now_ms=now_ms, presence=presence))


class Answered(NamedTuple):
    ref: str
    status: str
    # Сессия хаба, заведённая ответом (заявка агента «новая сессия»): маршрут после коммита — в реестр.
    session: held_mod.HubSession | None = None
    # Ф83-1: сессия, закрытая ответом («Перейти на запасной» — передача дел): после коммита — из реестра.
    replaced: int | None = None


def answer(
    conn,
    ref: str,
    *,
    action: str,
    text: str | None = None,
    answers: dict | None = None,
    send_now: bool = False,
    principal,
    source: str,
    now_ms: int,
    audit: dict,
    waiting=None,
    presence: list[dict] | None = None,
    result: str | None = None,
    word: dict | None = None,
    basis: str | None = None,
    config_dir=None,
    files: list[str] | None = None,
) -> Answered:
    """Ответ по карточке очереди — тем же кодом ядра, что кнопка владельца в интерфейсе (таблица раздела 4).

    `source`: `ui` — владелец (кука), `agent` — агент владельца (токен `human`). Порядок проверок: неизвестный `ref`
    или карточки нет — 404; карточка другого проекта токена — 404; агенту карточка не в канале — 403
    `not_in_channel` (Ф66-1, П2: агенту — маршрут `agent` ИЛИ открытое поручение «Реши» по карточке); действие
    не из `answers` вида или тело не по нему — 422; дальше функция ядра (уже решена или снята — её 409 как
    есть). Ответ агента: строка `owner_item` (`closed_by='agent'`) и аудит `owner_answer` без текстов.

    Ф65-1: ответ владельца по карточке с открытым поручением агенту закрывает поручение (`ui`, «ответил владелец») и
    исполняется в той же транзакции (Ж1; 409 `errand_open` Ф58-3 больше нет). Ответ агента закрывает своё
    поручение (`agent`) с итогом `result` (до 300, маскируется; Ж4) — итог лежит в поручении и в строке ответа
    агента (`GET /owner_channel/closed`, поле `errand_result`). `result` от владельца — 422.

    Ф75-1: `word` (только агент; от владельца — 422) — агент действует по реплике владельца в разговоре: слово владельца
    =
    его клик, поэтому карточка вне канала агента тоже проходит, если реплика годится (`owner_word.check`;
    сверяется всегда, когда `word` передан; отказ — 403 `word_rejected: …`). Аудит — с `word_talk_id`.

    Ф76-1 (П1, П5): каждый ответ по карточке-решению — строка журнала решений (`decisions.record`): владелец —
    `owner_ui`, агент — `agent`; ответ агента со словом владельца (`{talk_id}` или `{errand_id}` — указание владельца
    к «Реши» по этой карточке) — `agent_on_word` с текстом владельца.

    Файлы в ответе: `files` — id файлов, загруженных в сессию карточки (`POST /sessions/{id}/files`), только у
    реплики сессии (`turn` -> `reply`, карточки состояний -> `say`/`interrupt`); с файлами текст может быть
    пустым. Пределы и принадлежность файлов проверяет `held_work.say`; у прочих видов и действий — 422.

    Ф76-3: `basis` (только агент; от владельца — 422) — основание ответа агента (`decisions.check_basis`: правило
    владельца, образец, похожее решение, сама карточка, «нет»): строка журнала решений, строка ответа агента в
    `owner_item` и его поручение (`GET /owner_channel/closed`, поле `basis`). Ревью Ф76: основание сверяется по
    существу (`decisions.basis_checked`); образца, правила или похожих нет — пишется `none`, ответ идёт."""
    if source not in outbox_ops.SOURCES:
        raise ValueError(f"answer: источник {source!r}")
    if result is not None:
        if source != "agent":
            raise OwnerError(422, "result: итог пишет только агент")
        if not isinstance(result, str) or not result.strip() or len(result) > RESULT_MAX:
            raise OwnerError(422, f"result: 1–{RESULT_MAX} символов")
        result = _mask(" ".join(result.split()))
    if basis is not None:
        if source != "agent":
            raise OwnerError(422, "basis: основание пишет только агент")
        try:
            basis = decisions_mod.check_basis(basis)
        except decisions_mod.DecisionError as exc:
            raise OwnerError(exc.status, exc.detail) from exc
    presence = presence or []
    prefix, number = parse_ref(ref)
    loc = _locate(conn, prefix, number, now_ms, ref)
    if loc is None or not _visible(principal, loc.project_id):
        raise OwnerError(404, "not found")
    agent = source == "agent"
    if word is not None and not agent:
        raise OwnerError(422, "word: слово владельца передаёт только агент")
    word_audit: dict = {}
    if word is not None:
        word_audit = _word_check(conn, word, ref, principal, now_ms=now_ms, presence=presence)
    elif agent and not agent_may(conn, ref, loc.project_id, loc.hub_id, loc.kind, loc.sub):
        raise OwnerError(403, NOT_IN_CHANNEL)
    spec = next((a for a in _answers_of(loc.kind, loc.sub, all_=True) if a["action"] == action), None)
    if spec is None:
        allowed = ", ".join(a["action"] for a in _answers_of(loc.kind, loc.sub, all_=True))
        raise OwnerError(422, f"action: у карточки «{loc.kind}» — {allowed}")
    if files and not _takes_files(loc.kind, action):
        raise OwnerError(422, "files: файлы — только в реплике сессии")
    _check_body(spec, text, answers, send_now, loc.kind, has_files=bool(files))
    # Ревью Ф76: основание по существу (образец есть у карточки, правило есть текстом, похожие есть) — до
    # решения и записи; не прошло — ответ принимается с основанием `none`.
    basis = decisions_mod.basis_checked(conn, ref, basis, loc.project_id, now_ms=now_ms)
    by = decisions_mod.by_of_source(source)
    word_text = word_json = None
    if word_audit:
        from . import owner_word as word_mod  # импорт внутри: модуль слова опирается на этот

        word_text, word_json = word_mod.text_of(conn, word_audit)
        by = decisions_mod.BY_WORD
    if not agent:
        take_from_agent(conn, ref, now_ms=now_ms, token_id=principal.token_id)  # Ф65-1 (Ж1)
    with decisions_mod.acting(by, word_text, word_json, basis) as acted:
        status, session = _execute(
            conn,
            loc,
            action,
            text=text,
            answers=answers,
            send_now=send_now,
            principal=principal,
            source=source,
            now_ms=now_ms,
            audit=audit,
            waiting=waiting,
            presence=presence,
            ref=ref,
            by_word=bool(word_audit),
            config_dir=config_dir,
            files=files or None,
        )
    # Ф76-1 (П5, П6): ход сессии пишет только `close_turns` (реплика или «Принял» закрыли актуальную карточку
    # `turn`); «Принял» по вытесненной карточке ничего не закрывает — не решение.
    if loc.kind != "turn" and ref not in acted.refs:
        decisions_mod.record(
            conn,
            ref,
            by,
            action,
            word_text if word_text is not None else text,
            now_ms=now_ms,
            answers=answers,
            word=word_json,
            basis=basis,
        )
    if agent:
        _close_own_errand(conn, ref, now_ms=now_ms, token_id=principal.token_id, result=result, basis=basis)
    else:
        _close_errands_of(conn, ref, now_ms=now_ms)
    if agent and loc.kind not in _OWN_ROW_KINDS:  # у `turn` и состояний строка своя — её закрыл ответ
        conn.execute(
            "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, closed_ts, "
            "closed_by, closed_token_id, payload_json, seq) VALUES (?, ?, ?, ?, ?, ?, 'agent', ?, ?, ?)",
            (
                ref,
                loc.kind,
                loc.project_id,
                loc.hub_id,
                now_ms,
                now_ms,
                principal.token_id,
                json.dumps(
                    {
                        "action": action,
                        **({"result": result} if result is not None else {}),
                        **({"basis": basis} if basis is not None else {}),
                    }
                ),
                next_seq(conn),
            ),
        )
    if agent:
        write_audit(
            conn,
            now_ms=now_ms,
            actor="agent",
            api_token_id=principal.token_id,
            client_name=audit.get("client_name"),
            request_id=audit.get("request_id") or "",
            action="owner_answer",
            result=status,
            detail={
                "ref": ref,
                "kind": loc.kind,
                "action": action,
                "project_id": loc.project_id,
                **word_audit,
            },
        )
    replaced = loc.hub_id if loc.kind == "paused" and status == "switched" else None
    return Answered(ref, status, session, replaced)


def _word_check(conn, word: dict, ref: str, principal, *, now_ms: int, presence: list[dict]) -> dict:
    """Ф75-1: слово владельца для открытой карточки `ref` (её сессия и время создания — из очереди) -> поля
    аудита. Карточки нет среди открытых — 404 (как без слова)."""
    from . import owner_word as word_mod  # импорт внутри: модуль слова опирается на этот

    cards = _collect(conn, principal, now_ms=now_ms, presence=presence)
    card = next((c for c in cards if c["ref"] == ref), None)
    if card is None:
        raise OwnerError(404, "not found")
    return word_mod.check(
        conn,
        word,
        ref=ref,
        session_id=card["session_id"],
        created_ts=card["created_ts"],
        now_ms=now_ms,
        errand_ok=True,  # Ф76-1: в ответе по карточке — и указание владельца к «Реши»
    )


def _execute(
    conn,
    loc: _Loc,
    action: str,
    *,
    text,
    answers,
    send_now,
    principal,
    source,
    now_ms,
    audit,
    waiting,
    presence,
    ref,
    by_word: bool = False,
    config_dir=None,
    files: list[str] | None = None,
) -> tuple[str, held_mod.HubSession | None]:
    """Исполнить ответ функцией ядра вида; вернуть (статус после ответа, заведённая сессия хаба или
    `None`). Ф75-1: `by_word` — ответ агента по слову владельца (`owner_word.check` пройден): функции вида со
    своей сверкой маршрута агента (заявка «нужен человек») её пропускают — это клик владельца."""
    kind, number = loc.kind, loc.number
    client_name, request_id = audit.get("client_name"), audit.get("request_id") or ""
    if kind in ("permission", "question"):
        decision = "deny" if action == "deny" else "allow"
        status, _sid = held_work_mod.decide(
            conn,
            number,
            decision=decision,
            answers=answers if action == "answer" else None,
            message=text if action == "deny" else None,
            now_ms=now_ms,
            audit=audit,
        )
        return status, None
    if kind == "human":
        req = human_mod.decide(
            conn,
            number,
            action,
            principal=principal,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            by_word=by_word,
            note=text,
        )
        return req.status, None
    if kind == "draft":
        if action == "approve":
            outbox_ops.approve_draft(
                conn,
                number,
                principal=principal,
                text=text,
                send_now=send_now,
                now_ms=now_ms,
                source=source,
                audit=audit,
            )
            return "queued", None
        queries.outbox_visible(conn, principal, number)
        if outbox_mod.status_of(conn, number) != "pending_approval":
            raise outbox_mod.InvalidTransition("черновик уже не ждёт подтверждения")
        status = outbox_ops.cancel_draft(
            conn,
            number,
            principal=principal,
            owner_token_id=None,
            record_outcome=True,
            now_ms=now_ms,
            audit=audit,
        )
        return status, None
    if kind in ("press", "join", "leave", "group_op"):
        module = {"press": presses_mod, "join": join_mod, "leave": leave_mod, "group_op": group_ops_mod}[kind]
        req = module.decide(
            conn,
            number,
            action,
            now_ms=now_ms,
            api_token_id=principal.token_id,
            client_name=client_name,
            audit_request_id=request_id,
        )
        return req.status, None
    if kind == "rule":
        out = rule_requests_mod.decide(
            conn,
            number,
            action,
            now_ms=now_ms,
            api_token_id=principal.token_id,
            client_name=client_name,
            audit_request_id=request_id,
        )
        return out["status"], None
    if kind == "agent_request":
        decided = agent_requests_mod.decide(
            conn,
            number,
            action,
            note=text if action == "decline" else None,
            now_ms=now_ms,
            audit=audit,
            waiting=waiting,
        )
        return decided.request["status"], decided.session
    if kind == "reminder":
        rec = reminders_mod.get(conn, number)
        if rec is None or rec.status != "fired":
            raise OwnerError(409, "напоминание уже закрыто")
        if rec.every is not None and rec.missed < 1:
            # Ф89-1 (П1): срок повторяемого ведёт сама сессия — карточки нет, пока она не пропустила срок.
            raise OwnerError(409, "повторяемое напоминание: срок ведёт сессия, карточки нет")
        rec = reminders_mod.update(
            conn,
            number,
            principal=principal,
            session=None,
            at=None,
            note=None,
            cancel_if_reply=None,
            close=True,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            owner=True,
            stop=action == "stop",  # Ф89-1: «Снять» — остановить регламент; «Сделано» — следующий срок
        )
        return rec.status, None
    if kind in ("heavy", "paused"):
        if not _still_open(conn, principal, ref, now_ms=now_ms, presence=presence):
            raise OwnerError(409, "сессия уже не ждёт подъёма")
        if action == "fallback":
            # Ф83-1 (П6): «Перейти на запасной» — передача дел на запасной движок (`by: owner_channel`; ответ агента
            # через канал — тот же клик владельца). Отказ — 409 с текстом.
            try:
                new, _closed, _goal = engine_fallback_mod.switch_session(
                    conn,
                    loc.hub_id,
                    by="owner_channel",
                    kind=None,
                    now_ms=now_ms,
                    audit=audit,
                    waiting=waiting,
                    config_dir=config_dir,
                    closed_status=409,
                )
            except engine_fallback_mod.Refused as exc:
                raise OwnerError(409, exc.why) from exc
            return "switched", new
        held_signals_mod.wake(conn, loc.hub_id, now_ms=now_ms, presence=presence, audit=audit)
        return "wake_requested", None
    if kind == "turn":
        return _answer_turn(
            conn,
            loc,
            action,
            text=text,
            source=source,
            principal=principal,
            now_ms=now_ms,
            audit=audit,
            files=files,
        )
    if kind in STATE_KINDS:
        return _answer_state(
            conn,
            loc,
            action,
            text=text,
            source=source,
            principal=principal,
            now_ms=now_ms,
            audit=audit,
            presence=presence,
            files=files,
        )
    if kind == "agent_msg":
        return _answer_agent_msg(
            conn, number, action, text=text, principal=principal, now_ms=now_ms, audit=audit, waiting=waiting
        )
    # interrupted
    if action == "resend":
        held_work_mod.say_resend(conn, loc.hub_id, number, now_ms=now_ms, audit=audit)
        return "queued", None
    held_work_mod.say_remove(conn, loc.hub_id, number, now_ms=now_ms, audit=audit)
    return "dropped", None


def _answer_turn(
    conn, loc: _Loc, action: str, *, text, source: str, principal, now_ms: int, audit: dict, files=None
) -> tuple[str, None]:
    """3.5: `reply` — реплика сессии (`held_say` с источником ответа, срочность `next`; спящую разбудит хост,
    как реплику из окна); она же закрывает карточку. `ok` — закрыть без реплики. Ревью Ф56 (4), А6: карточку
    вытеснил следующий ход (`superseded`, ответа по ней не было) — `reply` принимается репликой сессии и
    закрывает актуальную карточку, `ok` — 200 без реплики; оба записываются в вытесненную строку. 409 — по
    этой карточке уже отвечали, актуальную карточку сессии уже закрыли ответом (круг 2 (1)) или сессия
    закрыта."""
    row = conn.execute(
        "SELECT closed_ts, closed_by, payload_json FROM main.owner_item WHERE id = ?", (loc.number,)
    ).fetchone()
    if row is None:
        raise OwnerError(409, "по карточке уже ответили или она снята")
    superseded = row[0] is not None and row[1] == "system" and _payload(row[2]).get("action") == "superseded"
    if row[0] is not None and not superseded:
        raise OwnerError(409, "по карточке уже ответили или она снята")
    if superseded and held_mod.get_open(conn, loc.hub_id) is None:
        raise OwnerError(409, "сессия закрыта")
    token_id = principal.token_id if source == "agent" else None
    if superseded:
        # Круг 2 (1): актуальную карточку сессии уже закрыли ответом (владелец или агент) — на ход ответили,
        # второй реплики по старой карточке не будет.
        last = conn.execute(
            "SELECT id, closed_ts, closed_by FROM main.owner_item WHERE kind = 'turn' AND session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (loc.hub_id,),
        ).fetchone()
        if last is not None and last[0] != loc.number and last[1] is not None and last[2] in ("ui", "agent"):
            raise OwnerError(409, "по карточке уже ответили или она снята")
        if source == "agent":
            # Ревью Ф93-1 (Б1): реплика по вытесненной карточке закрывает актуальную карточку сессии
            # (`held_work.say` -> `close_turns`) — агенту нельзя, если на актуальную он не вправе отвечать
            # (денежная при «Деньги» = «Мне», маршрут «Мне» без поручения). Владелец (кука, бот) — как прежде.
            open_refs = conn.execute(
                "SELECT ref FROM main.owner_item WHERE kind = 'turn' AND session_id = ? "
                "AND closed_ts IS NULL",
                (loc.hub_id,),
            ).fetchall()
            if any(not agent_may(conn, r, loc.project_id, loc.hub_id, "turn") for (r,) in open_refs):
                raise OwnerError(409, "актуальная карточка сессии — у владельца")
    say_id = None
    if action == "reply":
        say_id = held_work_mod.say(
            conn,
            loc.hub_id,
            text=text or "",
            files=files,
            priority="next",
            now_ms=now_ms,
            audit=audit,
            source=source,
            token_id=token_id,
            typed=source == "ui",  # Ф86-1 (П4): ответ кукой — набрал владелец; агент владельца — нет
        )
    if superseded:
        # Круг 2 (1): ответ принят — он записан в саму вытесненную строку (кто, действие, реплика): повтор
        # того же ответа по тому же ref — уже 409, а не вторая реплика сессии.
        payload = {**_payload(row[2]), "action": action}
        if say_id is not None:
            payload["say_id"] = say_id
        conn.execute(
            "UPDATE main.owner_item SET closed_by = ?, closed_token_id = ?, payload_json = ?, seq = ? "
            "WHERE id = ?",
            (source, token_id, json.dumps(payload, ensure_ascii=False), next_seq(conn), loc.number),
        )
        return ("queued" if action == "reply" else "closed"), None
    if action == "reply":
        return "queued", None
    close_turns(conn, loc.hub_id, by=source, action="ok", now_ms=now_ms, token_id=token_id)
    return "closed", None


def _answer_agent_msg(
    conn, msg_id: int, action: str, *, text, principal, now_ms: int, audit: dict, waiting
) -> tuple[str, None]:
    """4.2: `reply` — сообщение «ОТ ВЛАДЕЛЬЦА» автору тем же путём, что ответ владельца из интерфейса
    (`peer_msgs.reply_user`), затем исходное прочитано; `ok` — прочитано. Уже прочитано или истекло — 409."""
    msg = peer_msgs_mod.get(conn, msg_id)
    if msg is None or msg.status != "pending":
        raise OwnerError(409, "сообщение уже прочитано или истекло")
    client_name, request_id = audit.get("client_name"), audit.get("request_id") or ""
    if action == "reply":
        peer_msgs_mod.reply_user(
            conn,
            text=text,
            reply_to=msg_id,
            principal=principal,
            now_ms=now_ms,
            client_name=client_name,
            request_id=request_id,
            waiting=waiting,
        )
    peer_msgs_mod.mark_read(
        conn,
        [msg_id],
        principal=principal,
        sub=None,
        now_ms=now_ms,
        client_name=client_name,
        request_id=request_id,
    )
    return ("replied" if action == "reply" else "read"), None


# --- денежные карточки для бота (Ф93-1, раздел B.1) ---------------------------------------------------------

MONEY_WINDOW_MS = 24 * 60 * 60_000  # бот видит денежные карточки не старше суток (старше — подрезается)
MONEY_TEXT_MAX = 4000
MONEY_MAX = 50
MONEY_REPLY_MAX = 4000
MONEY_KINDS = ("turn", "agent_msg")
VIA_BOT = "bot"


class CardNotOpen(OwnerError):
    """Ответ из бота по карточке, которая не ждёт его ответа: `state` — `closed` (ответили, вытеснена,
    прочитана, сессия закрыта), `not_money` (не денежная и не возвращена агентом) или `agent` (строка
    «Деньги» — у агента)."""

    def __init__(self, state: str) -> None:
        super().__init__(409, f"card_not_open: карточка не ждёт ответа из бота ({state})")
        self.state = state


def _line_label(role: str | None, role_no, fallback: str | None) -> str | None:
    if role:
        return f"{role} {role_no}" if isinstance(role_no, int) and role_no > 1 else role
    return fallback


def _turn_card_text(payload: dict) -> str:
    return _clip_text(_mask(str(payload.get("full") or payload.get("brief") or "")), MONEY_TEXT_MAX)


class _DayGate:
    """Ревью Ф93-1 (В4), как Ф64-1 у заявок (`human_requests.readable`): открытые карточки проекта с закрытым
    днём боту не отдаются; появившиеся за последний сон проекта после «Начать день» отдаются со временем
    открытия дня — иначе фильтр бота «не старше часа» отрезал бы утром ночные. Время карточки, заведённой до
    закрытия дня, не меняется (бот сверяет `ts` отправленной и при отличии шлёт её заново)."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.closed = workday_mod.closed_projects(conn)
        self._slept: dict[int | None, tuple[int, int] | None] = {}

    def hidden(self, project_id: int | None) -> bool:
        return project_id in self.closed

    def ts(self, project_id: int | None, ts: int) -> int:
        if project_id not in self._slept:
            row = self.conn.execute(
                "SELECT agent_policy_json FROM main.project WHERE id = ?", (project_id,)
            ).fetchone()
            self._slept[project_id] = workday_mod.slept_of(row[0]) if row is not None else None
        slept = self._slept[project_id]
        if slept is not None and slept[0] <= ts < slept[1]:
            return slept[1]
        return ts


def _money_turns(conn, since: int, names: dict, gate: _DayGate) -> list[dict]:
    found = []
    rows = conn.execute(
        "SELECT id, ref, project_id, session_id, created_ts, closed_ts, payload_json FROM main.owner_item "
        "WHERE kind = 'turn' AND created_ts >= ? ORDER BY id DESC LIMIT ?",
        (since, MONEY_MAX * 4),
    ).fetchall()
    for _row_id, ref, project_id, hub_id, created_ts, closed_ts, raw in rows:
        payload = _payload(raw)
        if not card_route_mod.is_money("turn", payload):
            continue
        rec = held_mod.get(conn, hub_id) if hub_id is not None else None
        is_open = closed_ts is None and rec is not None and rec.closed_ts is None
        if (
            is_open
            and card_route_mod.for_card(conn, project_id, "turn", hub_id, ref=ref, money=True)
            != card_route_mod.ME
        ):
            continue
        if is_open and gate.hidden(project_id):
            continue
        title = _line_label(rec.role, rec.role_no, held_mod.shown_name(rec)) if rec is not None else None
        found.append(
            {
                "ref": ref,
                "ts": gate.ts(project_id, created_ts),
                "kind": "turn",
                "project": names.get(project_id),
                "title": title,
                "text": _turn_card_text(payload),
                "state": "open" if is_open else "closed",
            }
        )
    return found


def _money_agent_msgs(conn, since: int, names: dict, gate: _DayGate) -> list[dict]:
    found = []
    rows = conn.execute(
        "SELECT id FROM main.peer_msg WHERE to_user = 1 AND money = 1 AND created_ts >= ? AND id >= ? "
        "ORDER BY id DESC LIMIT ?",
        (since, agent_msg_from_id(conn), MONEY_MAX),
    ).fetchall()
    for (msg_id,) in rows:
        rec = peer_msgs_mod.get(conn, msg_id)
        if rec is None:
            continue
        ref = f"agent_msg:{rec.id}"
        is_open = rec.status == "pending"
        hub_id = _hub_of_sub(conn, rec.from_sub_id)
        if (
            is_open
            and card_route_mod.for_card(conn, rec.from_project_id, "agent_msg", hub_id, ref=ref, money=True)
            != card_route_mod.ME
        ):
            continue
        if is_open and gate.hidden(rec.from_project_id):
            continue
        found.append(
            {
                "ref": ref,
                "ts": gate.ts(rec.from_project_id, rec.created_ts),
                "kind": "agent_msg",
                "project": names.get(rec.from_project_id),
                "title": _line_label(rec.from_role, rec.from_no, None),
                "text": _clip_text(_mask(rec.text), MONEY_TEXT_MAX),
                "state": "open" if is_open else "closed",
            }
        )
    return found


def money_cards(conn, *, since_ms: int, now_ms: int) -> list[dict]:
    """Ф93-1 (B.1): бот — денежные карточки `turn` и `agent_msg` не старше `since_ms` (не раньше суток назад):
    открытые — только с маршрутом `me` (строка «Деньги» у владельца), закрытые — все (бот отмечает закрытие
    своих сообщений). `[{ref, ts, kind, project, title, text, state}]` по возрастанию `ts`; текст —
    маскированный, до `MONEY_TEXT_MAX`. Заявки `payment` сюда не входят — бот шлёт их с кнопками. Ревью
    Ф93-1 (В4): проект с закрытым днём — без открытых, время ночных — открытие дня (`_DayGate`)."""
    since = max(int(since_ms), now_ms - MONEY_WINDOW_MS)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM main.project")}
    gate = _DayGate(conn)
    found = [*_money_turns(conn, since, names, gate), *_money_agent_msgs(conn, since, names, gate)]
    found.sort(key=lambda c: (c["ts"], c["ref"]))
    return found[-MONEY_MAX:]


# --- карточки, которые агент вернул владельцу (Ф93-1, раздел F2) ------------------------------------------------

# Вид карточки по-русски — подпись возврата в боте.
KIND_RU = {
    "permission": "разрешение",
    "question": "вопрос",
    "human": "нужен человек",
    "draft": "черновик",
    "press": "кнопка бота",
    "join": "вступление",
    "leave": "выход",
    "group_op": "группа",
    "rule": "правило",
    "agent_request": "заявка",
    "reminder": "напоминание",
    "heavy": "тяжёлая",
    "paused": "пауза",
    "interrupted": "прерванная реплика",
    "turn": "ответ сессии",
    "broken": "ход оборван",
    "dead": "не запускается",
    "stuck": "завис",
    "stall": "стоит",
    "agent_msg": "сообщение агента",
}
REPLY_KINDS = ("turn", "agent_msg")  # ответ текстом из бота — только у них
RETURNED_BY = ("agent", "system")  # вернул агент (`errand_return`) или ядро за зависание (`return_hung`)


def _is_returned(conn, ref: str) -> bool:
    """Последнее поручение по карточке `ref` закрыто возвратом владельцу агентом или ядром (не владельцем)."""
    returned = _returned_errands(conn).get(ref)
    return returned is not None and returned["by"] in RETURNED_BY


def _returned_info(conn, ref: str, project_id: int | None, hub_id: int | None, names: dict) -> dict:
    """Вид, проект, подпись и текст карточки `ref` для сообщения о возврате — из строк базы (и у закрытой
    карточки), а не из очереди."""
    try:
        prefix, number = parse_ref(ref)
        loc = _locate(conn, prefix, number, 0, ref)
    except OwnerError:
        loc = None
    kind = loc.kind if loc is not None else ref.partition(":")[0]
    if loc is not None:
        project_id, hub_id = loc.project_id, loc.hub_id
    role = None
    text = None
    if kind == "agent_msg" and loc is not None:
        msg = peer_msgs_mod.get(conn, loc.number)
        if msg is not None:
            role = _line_label(msg.from_role, msg.from_no, None)
            text = _clip_text(_mask(msg.text), MONEY_TEXT_MAX)
    else:
        rec = held_mod.get(conn, hub_id) if hub_id is not None else None
        if rec is not None:
            role = _line_label(rec.role, rec.role_no, held_mod.shown_name(rec))
        if kind == "turn" and loc is not None:
            row = conn.execute(
                "SELECT payload_json FROM main.owner_item WHERE id = ?", (loc.number,)
            ).fetchone()
            text = _turn_card_text(_payload(row[0] if row is not None else None))
    label = KIND_RU.get(kind, kind)
    title = role if kind in REPLY_KINDS else " · ".join(p for p in (role, label) if p)
    return {
        "kind": kind,
        "project": names.get(project_id),
        "title": title or label,
        "text": text,
        "replyable": kind in REPLY_KINDS,
    }


def returned_cards(conn, *, since_ms: int, now_ms: int) -> list[dict]:
    """Ф93-1 (F2): бот — возвраты карточек владельцу («вернул», «завис», «не вышло»: поручение закрыто агентом или
    ядром, не владельцем) не старше `since_ms` (не раньше суток назад) -> `[{id, ref, ts, kind, project, title,
    reason, detail, text, replyable, state}]` по возрастанию `ts`. `id` — номер поручения (один возврат —
    одно сообщение бота); `state` — `open`, пока возврат последний по карточке и карточка открыта; `text` —
    полный текст `turn` / `agent_msg` (маскированный, до `MONEY_TEXT_MAX`), у прочих видов `null` (ответ
    текстом из бота — только у них: `replyable`)."""
    since = max(int(since_ms), now_ms - MONEY_WINDOW_MS)
    rows = conn.execute(
        _ERRAND_SELECT + "WHERE kind = 'errand' AND closed_ts IS NOT NULL AND closed_ts >= ? "
        "AND closed_by IN ('agent', 'system') ORDER BY id DESC LIMIT ?",
        (since, MONEY_MAX),
    ).fetchall()
    # Ф93-2 (B): возврат заявки «нужен человек» бот шлёт самой заявкой с кнопками (`at_owner`), не здесь.
    errands = [
        e
        for e in (_errand_row(r) for r in rows)
        if isinstance(e["target"], str)
        and not e["target"].startswith("human:")
        and isinstance(e["returned"], str)
        and e["returned"].strip()
    ]
    if not errands:
        return []
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM main.project")}
    current = _returned_errands(conn)
    open_refs = {c["ref"] for c in _collect(conn, _TITLE_VIEWER, now_ms=now_ms, presence=[])}
    gate = _DayGate(conn)
    found = []
    for errand in errands:
        ref = errand["target"]
        last = current.get(ref)
        is_open = last is not None and last["ts"] == errand["closed_ts"] and ref in open_refs
        if is_open and gate.hidden(errand["project_id"]):
            continue  # ревью Ф93-1 (В4): день проекта закрыт — боту утром
        found.append(
            {
                "id": errand["id"],
                "ref": ref,
                "ts": gate.ts(errand["project_id"], errand["closed_ts"]),
                **_returned_info(conn, ref, errand["project_id"], errand["session_id"], names),
                "reason": errand["returned"],
                "detail": errand["returned_detail"] if isinstance(errand["returned_detail"], str) else None,
                "state": "open" if is_open else "closed",
            }
        )
    found.sort(key=lambda c: (c["ts"], c["id"]))
    return found


# --- ответ владельца из бота по карточке (денежной или возвращённой агентом) -----------------------------------


def _reply_repeat(conn, ref: str, tg_message_id: int) -> str | None:
    """Ответ из бота с этим `tg_message_id` по этой карточке уже принят — его статус (`queued` /
    `replied`)."""
    row = conn.execute(
        "SELECT payload_json FROM main.owner_item WHERE ref = ? AND kind IN ('turn', 'agent_msg') "
        "AND closed_ts IS NOT NULL ORDER BY id DESC LIMIT 1",
        (ref,),
    ).fetchone()
    if row is None:
        return None
    payload = _payload(row[0])
    ids = payload.get("bot_msg_ids")
    if not isinstance(ids, list) or tg_message_id not in ids:
        return None
    return "queued" if ref.startswith("turn:") else "replied"


def _reply_state(conn, loc: _Loc, ref: str) -> tuple[str, bool, bool]:
    """Ждёт ли карточка ответа из бота -> (`open` | `closed` | `not_money` | `agent`, денежная, возвращена).
    Возвращённая агентом — у владельца при любой строке «Деньги»; денежная — по строке «Деньги»."""
    if loc.kind == "turn":
        row = conn.execute(
            "SELECT closed_ts, payload_json FROM main.owner_item WHERE id = ?", (loc.number,)
        ).fetchone()
        if row is None:
            return "closed", False, False
        money = card_route_mod.is_money("turn", _payload(row[1]))
        rec = held_mod.get(conn, loc.hub_id) if loc.hub_id is not None else None
        closed = row[0] is not None or rec is None or rec.closed_ts is not None
    else:
        msg = peer_msgs_mod.get(conn, loc.number)
        if msg is None:
            return "not_money", False, False
        money = card_route_mod.is_money("agent_msg", msg)
        closed = msg.status != "pending"
    returned = _is_returned(conn, ref)
    if not money and not returned:
        return "not_money", money, returned
    if closed:
        return "closed", money, returned
    if returned:
        return "open", money, returned
    route = card_route_mod.for_card(conn, loc.project_id, loc.kind, loc.hub_id, ref=ref, money=True)
    return ("open" if route == card_route_mod.ME else "agent"), money, returned


def card_reply(
    conn,
    *,
    ref: str,
    text: str,
    tg_message_id: int,
    principal,
    now_ms: int,
    audit: dict,
    waiting=None,
    presence: list[dict] | None = None,
) -> dict:
    """Ф93-1 (B.1, F2): ответ владельца из бота по карточке `turn` / `agent_msg`, которая у него — денежная (строка
    «Деньги» у владельца) или возвращённая агентом, — = его ответ в интерфейсе: `turn` — реплика сессии
    (`_answer_turn`, `typed`), `agent_msg` — сообщение «ОТ ВЛАДЕЛЬЦА» автору (`_answer_agent_msg`). В базе ответ
    записан как ответ владельца (`ui`: столбцы источника знают только `ui`/`agent`); что он из бота — `via: "bot"`
    и `bot_msg_ids` в строке карточки и аудит `owner_answer` с `via=bot`. Повтор с тем же `tg_message_id` по
    тому же `ref` — тот же ответ без второй реплики. Нет карточки — 404; не денежная и не возвращённая, не у
    владельца или закрыта — `CardNotOpen` (409 со `state`); текст 1–`MONEY_REPLY_MAX` — 422."""
    if isinstance(tg_message_id, bool) or not isinstance(tg_message_id, int) or tg_message_id < 1:
        raise OwnerError(422, "tg_message_id: целое > 0")
    prefix, number = parse_ref(ref)
    if prefix not in MONEY_KINDS:
        raise OwnerError(404, "not found")
    loc = _locate(conn, prefix, number, now_ms, ref)
    if loc is None:
        raise OwnerError(404, "not found")
    repeat = _reply_repeat(conn, ref, tg_message_id)
    if repeat is not None:
        return {"ref": ref, "status": repeat, "repeat": True}
    state, money, returned = _reply_state(conn, loc, ref)
    if state != "open":
        raise CardNotOpen(state)
    if not isinstance(text, str) or not text.strip():
        raise OwnerError(422, "text: пустой ответ")
    if len(text) > MONEY_REPLY_MAX:
        raise OwnerError(422, f"text: не длиннее {MONEY_REPLY_MAX}")
    done = answer(
        conn,
        ref,
        action="reply",
        text=text,
        principal=principal,
        source="ui",
        now_ms=now_ms,
        audit=audit,
        waiting=waiting,
        presence=presence or [],
    )
    mark = {"via": VIA_BOT, "bot_msg_ids": [tg_message_id]}
    if loc.kind == "turn":
        row = conn.execute("SELECT payload_json FROM main.owner_item WHERE id = ?", (loc.number,)).fetchone()
        payload = {**_payload(row[0] if row is not None else None), **mark}
        conn.execute(
            "UPDATE main.owner_item SET payload_json = ?, seq = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), next_seq(conn), loc.number),
        )
    else:
        conn.execute(
            "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, closed_ts, "
            "closed_by, closed_token_id, payload_json, seq) "
            "VALUES (?, 'agent_msg', ?, ?, ?, ?, 'ui', ?, ?, ?)",
            (
                ref,
                loc.project_id,
                loc.hub_id,
                now_ms,
                now_ms,
                principal.token_id,
                json.dumps({"action": "reply", **mark}, ensure_ascii=False),
                next_seq(conn),
            ),
        )
    write_audit(
        conn,
        now_ms=now_ms,
        actor=principal.kind,
        api_token_id=principal.token_id,
        client_name=audit.get("client_name"),
        request_id=audit.get("request_id") or "",
        action="owner_answer",
        result=done.status,
        detail={
            "ref": ref,
            "kind": loc.kind,
            "action": "reply",
            "project_id": loc.project_id,
            "via": VIA_BOT,
            "money": money,
            "returned": returned,
        },
    )
    return {"ref": ref, "status": done.status, "repeat": False}


# --- изменения (длинный опрос) ------------------------------------------------------------------------------


def watermark(conn) -> int:
    """Текущее значение глобального `seq` — отметка клиента для следующего опроса."""
    return conn.execute("SELECT value FROM main.seq_counter WHERE id = 1").fetchone()[0]


def has_changes(conn, since: int) -> bool:
    """Что-то в хабе изменилось после отметки клиента `since` (все таблицы-источники двигают общий `seq`;
    отметка из будущего — база восстановлена из копии — тоже «да»: клиент возьмёт новую из ответа)."""
    value = watermark(conn)
    return value > since or since > value


# --- состояние агента и его действия (П17) ------------------------------------------------------------------


def _agent_raw(conn) -> dict:
    raw = engine_mod._meta_get(conn, AGENT_META)  # noqa: SLF001
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _agent_save(conn, value: dict) -> None:
    engine_mod._meta_set(conn, AGENT_META, json.dumps(value, ensure_ascii=False))  # noqa: SLF001


def agent_state(conn, now_ms: int) -> dict:
    """`{online, seen_ts, actions}`: агент на связи — опрашивал очередь не дольше минуты назад; не на связи —
    действий в карточках нет («агент не на связи»)."""
    raw = _agent_raw(conn)
    seen = raw.get("seen_ts") if isinstance(raw.get("seen_ts"), int) else None
    online = seen is not None and 0 <= now_ms - seen <= ONLINE_MS
    actions = [a for a in raw.get("actions") or [] if isinstance(a, dict)]
    return {"online": online, "seen_ts": seen, "actions": actions if online else []}


def agent_seen_due(conn, principal, now_ms: int) -> bool:
    """Читатель: опрос агента пора отметить (давно не отмечали или опрашивает другой токен)."""
    raw = _agent_raw(conn)
    seen = raw.get("seen_ts")
    if raw.get("token_id") != principal.token_id or not isinstance(seen, int):
        return True
    return not 0 <= now_ms - seen < SEEN_WRITE_MS


def agent_seen(conn, principal, now_ms: int) -> None:
    """Опрос очереди агентом владельца: отметка «на связи» (`seq` не двигается: владельцу это не новость). Опрашивает
    другой токен — объявленные действия прежнего снимаются."""
    raw = _agent_raw(conn)
    if raw.get("token_id") != principal.token_id:
        raw = {"token_id": principal.token_id, "actions": []}
    raw["seen_ts"] = now_ms
    _agent_save(conn, raw)


# Ф78-1: виды карточек задач по «Го» убраны; агент прежней версии ещё может назвать их в `kinds` действия —
# такие виды молча отбрасываются (действие только с ними — тоже: «пусто» значило бы «все виды»).
RETIRED_KINDS = ("work_request", "proposal")


def _action_form(item) -> dict | None:
    if not isinstance(item, dict):
        raise OwnerError(422, "actions: список {id, label, kinds, needs_text}")
    extra = set(item) - {"id", "label", "kinds", "needs_text"}
    if extra:
        raise OwnerError(422, f"actions: лишние поля {sorted(extra)}")
    action_id, label = item.get("id"), item.get("label")
    kinds, needs_text = item.get("kinds", []), item.get("needs_text", False)
    if not isinstance(action_id, str) or not ACTION_ID_RE.fullmatch(action_id):
        raise OwnerError(422, "actions: id — [a-z0-9_-], 1–32 символа")
    if not isinstance(label, str) or not label.strip() or len(label) > ACTION_LABEL_MAX:
        raise OwnerError(422, f"actions: label — 1–{ACTION_LABEL_MAX} символов")
    if not isinstance(kinds, list) or not all(
        isinstance(k, str) and (k in KINDS or k in RETIRED_KINDS) for k in kinds
    ):
        raise OwnerError(422, f"actions: kinds — из {', '.join(KINDS)} (пусто — все)")
    if not isinstance(needs_text, bool):
        raise OwnerError(422, "actions: needs_text — true или false")
    live = [k for k in kinds if k not in RETIRED_KINDS]
    if kinds and not live:
        return None
    kinds = live
    return {
        "id": action_id,
        "label": " ".join(label.split()),
        "kinds": sorted(set(kinds)),
        "needs_text": needs_text,
    }


def set_agent_actions(conn, principal, actions, now_ms: int) -> dict:
    """Агент объявляет свои действия (кнопки поручений): до 20, `id` уникальны. Двигает `seq` (интерфейс
    перерисует кнопки) и отмечает «на связи». Возвращает состояние (`agent_state`)."""
    if not isinstance(actions, list) or len(actions) > ACTIONS_MAX:
        raise OwnerError(422, f"actions: список не длиннее {ACTIONS_MAX}")
    forms = [f for f in (_action_form(a) for a in actions) if f is not None]
    if len({f["id"] for f in forms}) != len(forms):
        raise OwnerError(422, "actions: id повторяется")
    _agent_save(conn, {"token_id": principal.token_id, "actions": forms, "seen_ts": now_ms, "set_ts": now_ms})
    next_seq(conn)
    return agent_state(conn, now_ms)


# --- поручения агенту (П17) ---------------------------------------------------------------------------------


def _errand_row(row) -> dict:
    try:
        payload = json.loads(row[5]) if row[5] else {}
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    return {
        "id": row[0],
        "project_id": row[1],
        "session_id": row[2],
        "created_ts": row[3],
        "closed_ts": row[4],
        "target": payload.get("target"),
        "action": payload.get("action"),
        "label": payload.get("label"),
        "text": payload.get("text"),
        "progress": payload.get("progress"),
        # Ф65-1 (Ж2): время последнего шага; до первого шага — взятие.
        "progress_ts": payload.get("progress_ts") if isinstance(payload.get("progress_ts"), int) else row[3],
        "returned": payload.get("returned"),
        "returned_detail": payload.get("returned_detail"),
        "result": payload.get("result"),
        "auto": payload.get("auto") is True,
        "closed_by": row[6],
        # Ф96-1 (A): руки — у агента (по умолчанию) или у владельца.
        "hands": "owner_channel" if payload.get("hands") == "owner_channel" else "agent",
        "hands_ts": _int_or_none(payload.get("hands_ts")),
        "hands_by": payload.get("hands_by") if payload.get("hands_by") in ("ui", "bot", "agent") else None,
        "hands_why": payload.get("hands_why") if isinstance(payload.get("hands_why"), str) else None,
        "hands_note": payload.get("hands_note") if isinstance(payload.get("hands_note"), str) else None,
        "_payload": payload,
    }


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_ERRAND_SELECT = (
    "SELECT id, project_id, session_id, created_ts, closed_ts, payload_json, closed_by FROM main.owner_item "
)


def _open_errands(conn) -> dict[str, dict]:
    """Открытые поручения по `ref` целевой карточки."""
    rows = conn.execute(_ERRAND_SELECT + "WHERE kind = 'errand' AND closed_ts IS NULL ORDER BY id").fetchall()
    found: dict[str, dict] = {}
    for row in rows:
        errand = _errand_row(row)
        if isinstance(errand["target"], str):
            found[errand["target"]] = errand
    return found


def errand_on(conn, ref: str) -> bool:
    """Ф66-1 (П2): по карточке `ref` открыто поручение агенту («Реши») — и переехавшее с неё
    (`prev_targets`, `_carry_errands`)."""
    errands = _open_errands(conn)
    if ref in errands:
        return True
    return any(ref in (e["_payload"].get("prev_targets") or []) for e in errands.values())


def _errand_of(conn, ref: str) -> dict | None:
    """Открытое поручение по карточке `ref` (и переехавшее с неё, как `errand_on`) или `None`."""
    errands = _open_errands(conn)
    if ref in errands:
        return errands[ref]
    return next((e for e in errands.values() if ref in (e["_payload"].get("prev_targets") or [])), None)


def agent_may(
    conn, ref: str, project_id: int | None, hub_id: int | None, kind: str, sub: str | None = None
) -> bool:
    """Ф66-1 (П2): агенту владельца карточка доступна — маршрут `agent` по таблице ИЛИ открытое поручение по
    ней. Ф76-1 (П7): «всегда меня» снимает маршрут `agent`; «Реши» владельца по такой карточке — его слово.
    Ревью Ф76: поручение, которое агент взял себе сам (`auto`, без слова владельца), «всегда меня» не обходит.
    Ревью Ф93-1 (Б2): денежная карточка при строке «Деньги» = «Мне» агенту недоступна никогда."""
    if in_channel(conn, project_id, hub_id, kind, sub, ref=ref):
        return True
    if card_route_mod.money_of(conn, kind, ref) and _money_mine(conn, project_id):
        return False  # ревью Ф93-1 (Б2): денежная при «Деньги» = «Мне» — только владелец, и при поручении
    errand = _errand_of(conn, ref)
    if errand is None:
        return False
    payload = errand["_payload"]
    by_owner = payload.get("auto") is not True or payload.get("word_talk_id") is not None
    return by_owner or not decisions_mod.always_me_card(conn, project_id, ref)


_HANDS_PUBLIC = ("hands", "hands_by", "hands_ts", "hands_why")


def _errand_public(errand: dict) -> dict:
    """Поручение в выдаче (карточка у владельца, заявка боту, ответы ручек агента). Ф96-1 (A.5): с руками."""
    return {
        k: errand[k]
        for k in (
            "id",
            "action",
            "label",
            "text",
            "created_ts",
            "progress",
            "progress_ts",
            "auto",
            *_HANDS_PUBLIC,
        )
    }


def _returned_errands(conn) -> dict[str, dict]:
    """`{ref целевой карточки: {reason, detail, ts, by}}` — последнее поручение по карточке вернулось владельцу:
    агент вернул (`by='agent'`, Ф58-3), ядро — за зависание (`system`, Ф65-1 Ж2), владелец остановил агента или
    ответил сам (`ui`, Ж1). Новое поручение после возврата (открытое или закрытое) причину прячет."""
    rows = conn.execute(_ERRAND_SELECT + "WHERE kind = 'errand' ORDER BY id").fetchall()
    last: dict[str, dict] = {}
    for row in rows:
        errand = _errand_row(row)
        if isinstance(errand["target"], str):
            last[errand["target"]] = errand
    return {
        ref: {
            "reason": e["returned"],
            "detail": e["returned_detail"] if isinstance(e["returned_detail"], str) else None,
            "ts": e["closed_ts"],
            "by": e["closed_by"],
        }
        for ref, e in last.items()
        if e["closed_ts"] is not None
        and e["closed_by"] in ("agent", "system", "ui")
        and isinstance(e["returned"], str)
    }


def take_from_agent(conn, ref: str, *, now_ms: int, token_id: int | None = None) -> int:
    """Ф65-1 (Ж1): владелец (кука) или его бот отвечает по карточке, которую решает агент, — открытое поручение
    закрывается (`closed_by='ui'`, причина «ответил владелец»); дальше ответ исполняется как обычно В ТОЙ ЖЕ
    транзакции (отказ ответа откатывает и закрытие). Агент узнаёт по 409 на свой прогресс и
    останавливается. Агенту не зовётся. Возвращает число закрытых (0 или 1)."""
    errand = _open_errands(conn).get(ref)
    if errand is None:
        return 0
    payload = {**errand["_payload"], "returned": TAKEN_BY_OWNER}
    _close_errand(conn, errand, "ui", now_ms=now_ms, token_id=token_id, payload=payload)
    write_audit(
        conn,
        now_ms=now_ms,
        actor="core",
        api_token_id=token_id,
        client_name=None,
        request_id="",
        action="owner_errand",
        result="taken_by_owner",
        detail={"errand_id": errand["id"], "ref": ref},
    )
    return 1


def human_errands(conn) -> dict[int, dict]:
    """Ф58-3: поручения по заявкам «нужен человек» — `{id заявки: {errand, errand_returned}}` (только заявки,
    у которых есть хоть одно из двух). Выдача заявки владельцу и его боту (`human_requests.out_dict`)."""
    found: dict[int, dict] = {}
    for ref, errand in _open_errands(conn).items():
        m = _REF_RE.match(ref)
        if m is not None and m.group(1) == "human":
            found.setdefault(int(m.group(2)), {})["errand"] = _errand_public(errand)
    for ref, returned in _returned_errands(conn).items():
        m = _REF_RE.match(ref)
        if m is not None and m.group(1) == "human":
            found.setdefault(int(m.group(2)), {})["errand_returned"] = returned
    return {
        human_id: {"errand": v.get("errand"), "errand_returned": v.get("errand_returned")}
        for human_id, v in found.items()
    }


def _errand_card(errand: dict, target: dict) -> dict:
    """Поручение агенту — карточкой `errand` (только агенту): закрывается ответом по целевой карточке или
    возвратом (`errand_return`). Ф66-1 (П2): у карточки поручения всегда `in_channel=true`."""
    return {
        **target,
        "in_channel": True,
        "ref": f"errand:{errand['id']}",
        "kind": ERRAND_KIND,
        "brief": _brief(errand["label"], errand["text"]),
        "full": {
            "target": errand["target"],
            "action": errand["action"],
            "label": errand["label"],
            "text": errand["text"],
            "progress": errand["progress"],
            "progress_ts": errand["progress_ts"],
            "auto": errand["auto"],
            **{k: errand[k] for k in (*_HANDS_PUBLIC, "hands_note")},  # Ф96-1
        },
        "created_ts": errand["created_ts"],
        "until_ts": None,
        "answers": [],
        "agent_actions": [],
        "errand": None,
        "errand_returned": None,
    }


def _get_errand(conn, errand_id: int) -> dict:
    row = conn.execute(_ERRAND_SELECT + "WHERE id = ? AND kind = 'errand'", (errand_id,)).fetchone()
    if row is None:
        raise OwnerError(404, "not found")
    return _errand_row(row)


def _bump_human(conn, ref) -> None:
    """Ф93-2 (B): поручение по заявке «нужен человек» открылось или закрылось — новый `seq` заявки (бот
    узнаёт, у владельца ли она теперь)."""
    m = _REF_RE.match(ref) if isinstance(ref, str) else None
    if m is not None and m.group(1) == "human":
        human_mod.bump(conn, int(m.group(2)))


def _close_errand(conn, errand: dict, by: str, *, now_ms: int, token_id=None, payload=None) -> None:
    from . import owner_talk as talk_mod  # импорт внутри: разговор сам опирается на этот модуль

    _bump_human(conn, errand["target"])

    # 24.10: вопросы агента по этому поручению никто не ждёт — закрыть, чтобы бот не путал их со свежими.
    prev = [r for r in errand["_payload"].get("prev_targets") or [] if isinstance(r, str)]
    talk_mod.close_asks_of(conn, [errand["target"], *prev], now_ms=now_ms)
    conn.execute(
        "UPDATE main.owner_item SET closed_ts = ?, closed_by = ?, closed_token_id = ?, payload_json = ?, "
        "seq = ? WHERE id = ?",
        (
            now_ms,
            by,
            token_id,
            json.dumps(payload if payload is not None else errand["_payload"], ensure_ascii=False),
            next_seq(conn),
            errand["id"],
        ),
    )


def _close_own_errand(
    conn, ref: str, *, now_ms: int, token_id: int | None, result: str | None, basis: str | None = None
) -> int:
    """Ф65-1 (Ж4): агент ответил по карточке — его открытое поручение закрывается `agent` с итогом. Ф76-3:
    и с основанием ответа (`basis`), если агент его назвал."""
    errands = _open_errands(conn)
    errand = errands.get(ref)
    if errand is None:  # поручение переехало с вытесненной карточки `turn` (`_carry_errands`)
        errand = next((e for e in errands.values() if ref in (e["_payload"].get("prev_targets") or [])), None)
    if errand is None:
        return 0
    payload = {**errand["_payload"], "result": result, **({"basis": basis} if basis is not None else {})}
    _close_errand(conn, errand, "agent", now_ms=now_ms, token_id=token_id, payload=payload)
    return 1


def close_own_errand(conn, ref: str, *, now_ms: int, token_id: int | None, result: str | None = None) -> int:
    """Ф88-1 (П3): агент владельца сделал действие над сессией ручкой окна (`/sessions/*`) по карточке `ref` —
    его открытое поручение по ней закрывается `agent` (а не `ui`, как у куки через `take_from_agent`) и не
    возвращается потом как «завис». Нет поручения — 0."""
    return _close_own_errand(conn, ref, now_ms=now_ms, token_id=token_id, result=result)


def close_own_session_errands(conn, hub_session_id: int, *, now_ms: int, token_id: int | None) -> int:
    """Ф88-1 (П3): реплика агента в окно сессии закрывает её открытые карточки `turn`/`stall`
    (`held_work.say`) — его поручения по ним закрываются `agent`. Зовётся до реплики в той же транзакции."""
    rows = conn.execute(
        "SELECT ref FROM main.owner_item WHERE kind IN ('turn', 'stall') AND session_id = ? "
        "AND closed_ts IS NULL ORDER BY id",
        (hub_session_id,),
    ).fetchall()
    return sum(close_own_errand(conn, ref, now_ms=now_ms, token_id=token_id) for (ref,) in rows)


def _close_errands_of(conn, ref: str, *, now_ms: int) -> int:
    errand = _open_errands(conn).get(ref)
    if errand is None:
        return 0
    _close_errand(conn, errand, "system", now_ms=now_ms)
    return 1


def errand_create(
    conn,
    ref: str,
    action_id: str | None,
    text: str | None,
    *,
    principal,
    now_ms: int,
    audit: dict,
    presence: list[dict] | None = None,
    word: dict | None = None,
) -> dict:
    """Владелец (кука) поручает карточку агенту: одно открытое поручение на карточку (второе — 409); карточка
    открыта (нет — 404; Ф66-1 B.2: «Реши» — у любой карточки, маршрут не важен); действие — из объявленных
    агентом и подходящих к виду, агент на связи (иначе 409). Без действия — свободный текст (обязателен).

    Авторешение (Ф56-brain-05, слово владельца): агент (токен `human`) берёт карточку своего канала сам —
    поручением себе без действия и без текста (`auto: true`, подпись «решаю сам»). Дальше всё как у «Реши»:
    ход в карточке, возврат владельцу, владелец может отозвать. Ф65-1: ответ владельца поручение закрывает (Ж1);
    карточку
    не своего маршрута (Ф66-1: не `agent` по таблице) агент себе не берёт — 409 `auto_off` (Ж6); карточку,
    вернувшуюся владельцу (агент вернул, завис, владелец остановил), — тоже: 409 `auto_returned` (Ж7).

    Ф75-1: `word` (только агент) — «Реши» по слову владельца в разговоре = клик владельца «Реши»: годная реплика
    (`owner_word.check`) снимает `auto_off` и `auto_returned`; подпись поручения — «по твоему слову в
    разговоре», в поручении и аудите — `word_talk_id`."""
    auto = is_agent(principal)
    if word is not None and not auto:
        raise OwnerError(422, "word: слово владельца передаёт только агент")
    if auto and (action_id is not None or text is not None):
        raise OwnerError(422, "агент берёт карточку себе без действия и текста")
    card = next(
        (c for c in _collect(conn, principal, now_ms=now_ms, presence=presence or []) if c["ref"] == ref),
        None,
    )
    if card is None:
        parse_ref(ref)
        raise OwnerError(404, "not found")
    if ref in _open_errands(conn):
        raise OwnerError(409, "по карточке уже есть поручение")
    word_audit: dict = {}
    if word is not None:
        from . import owner_word as word_mod  # импорт внутри: модуль слова опирается на этот

        word_audit = word_mod.check(
            conn, word, ref=ref, session_id=card["session_id"], created_ts=card["created_ts"], now_ms=now_ms
        )
    if auto and not word_audit and not card["in_channel"]:
        raise OwnerError(409, AUTO_OFF)
    if auto and not word_audit and ref in _returned_errands(conn):
        raise OwnerError(409, AUTO_RETURNED)
    if text is not None and len(text) > ERRAND_TEXT_MAX:
        raise OwnerError(422, f"text: не длиннее {ERRAND_TEXT_MAX}")
    label = None
    if action_id is not None:
        state = agent_state(conn, now_ms)
        if not state["online"]:
            raise OwnerError(409, "агент не на связи")
        action = next((a for a in state["actions"] if a["id"] == action_id), None)
        if action is None or (action["kinds"] and card["kind"] not in action["kinds"]):
            raise OwnerError(422, "action: агент такого действия для этой карточки не объявлял")
        if action["needs_text"] and not text:
            raise OwnerError(422, "text: у этого действия нужен текст")
        label = action["label"]
    elif auto:
        label = WORD_LABEL if word_audit else AUTO_LABEL
    elif not text:
        raise OwnerError(422, "text: поручение без действия — текстом")
    payload = {
        "target": ref,
        "action": action_id,
        "label": label,
        "text": text,
        "progress": None,
        "progress_ts": now_ms,
        "returned": None,
        "auto": auto,
        **word_audit,
    }
    cur = conn.execute(
        "INSERT INTO main.owner_item (ref, kind, project_id, session_id, created_ts, payload_json, seq) "
        "VALUES ('errand:new', 'errand', ?, ?, ?, ?, ?)",
        (
            card["project_id"],
            card["session_id"],
            now_ms,
            json.dumps(payload, ensure_ascii=False),
            next_seq(conn),
        ),
    )
    errand_id = cur.lastrowid
    conn.execute("UPDATE main.owner_item SET ref = ? WHERE id = ?", (f"errand:{errand_id}", errand_id))
    _bump_human(conn, ref)
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_errand",
        result="created",
        detail={"errand_id": errand_id, "ref": ref, "action": action_id, "auto": auto, **word_audit},
        **audit,
    )
    return _errand_public(_get_errand(conn, errand_id))


def _agent_errand(conn, errand_id: int, principal) -> dict:
    errand = _get_errand(conn, errand_id)
    if not _visible(principal, errand["project_id"]):
        raise OwnerError(404, "not found")
    if errand["closed_ts"] is not None:
        raise OwnerError(409, "поручение уже закрыто")
    return errand


def errand_progress(conn, errand_id: int, text: str, *, principal, now_ms: int) -> dict:
    """Агент: строка хода поручения («ввожу код…»), до 200 символов, маскируется. Ф65-1 (Ж2): шаг отодвигает
    срок зависания (`progress_ts`); закрытое поручение (владелец ответил сам, ядро вернуло) — 409: агент
    останавливается."""
    if not isinstance(text, str) or not text.strip() or len(text) > PROGRESS_MAX:
        raise OwnerError(422, f"text: 1–{PROGRESS_MAX} символов")
    errand = _agent_errand(conn, errand_id, principal)
    payload = {**errand["_payload"], "progress": _mask(" ".join(text.split())), "progress_ts": now_ms}
    conn.execute(
        "UPDATE main.owner_item SET payload_json = ?, seq = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), next_seq(conn), errand_id),
    )
    return _errand_public(_get_errand(conn, errand_id))


def errand_return(
    conn, errand_id: int, reason: str, *, principal, now_ms: int, audit: dict, detail: str | None = None
) -> dict:
    """Агент возвращает поручение («не вышло: …»): поручение закрыто агентом, карточка снова у владельца. Ф65-1
    (Ж3): `detail` — что пробовал и что нужно от владельца (до 2000, маскируется, переносы строк сохраняются)."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > RETURN_MAX:
        raise OwnerError(422, f"reason: 1–{RETURN_MAX} символов")
    if detail is not None and (not isinstance(detail, str) or len(detail) > RETURN_DETAIL_MAX):
        raise OwnerError(422, f"detail: не длиннее {RETURN_DETAIL_MAX}")
    errand = _agent_errand(conn, errand_id, principal)
    payload = {
        **errand["_payload"],
        "returned": _mask(" ".join(reason.split())),
        "returned_detail": _mask(detail.strip()) if detail is not None and detail.strip() else None,
    }
    _close_errand(conn, errand, "agent", now_ms=now_ms, token_id=principal.token_id, payload=payload)
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_errand",
        result="returned",
        detail={"errand_id": errand_id, "ref": errand["target"]},
        **audit,
    )
    return {
        **_errand_public(_get_errand(conn, errand_id)),
        "returned": payload["returned"],
        "returned_detail": payload["returned_detail"],
    }


def errand_cancel(conn, errand_id: int, *, now_ms: int, audit: dict) -> None:
    """Владелец отзывает поручение («Остановить агента»). Уже закрытое — 409. Ф65-1: карточка показывает
    возврат `{reason: «остановил владелец», by: 'ui'}`, агент её сам больше не берёт (Ж7)."""
    errand = _get_errand(conn, errand_id)
    if errand["closed_ts"] is not None:
        raise OwnerError(409, "поручение уже закрыто")
    _close_errand(
        conn, errand, "ui", now_ms=now_ms, payload={**errand["_payload"], "returned": STOPPED_BY_OWNER}
    )
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_errand",
        result="cancelled",
        detail={"errand_id": errand_id, "ref": errand["target"]},
        **audit,
    )


def stale_errands(conn, principal, *, now_ms: int, presence: list[dict] | None = None) -> list[int]:
    """Читатель: открытые поручения проектов, видимых принципалу, чья карточка закрыта другим путём (владелец
    ответил в интерфейсе, карточка протухла) — их закрывает `close_errands` отдельной записью."""
    errands = [e for e in _open_errands(conn).values() if _visible(principal, e["project_id"])]
    if not errands:
        return []
    open_refs = {c["ref"] for c in _collect(conn, principal, now_ms=now_ms, presence=presence or [])}
    return [e["id"] for e in errands if e["target"] not in open_refs]


ASK_OPS_MAX = 50  # Ф85-1 (E): ключей операций вопросов в одном поручении
ASK_OP_KEY_MAX = 100


def errand_ask(
    conn,
    errand_id: int,
    *,
    operation_key,
    text,
    ask_ttl_s=None,
    principal,
    now_ms: int,
    audit: dict,
) -> dict:
    """Ф85-1 (E): агент задаёт владельцу вопрос по своему открытому поручению — реплика-вопрос разговора
    (`owner_talk.post`, `ask=true`, `ref` — целевая карточка поручения) и `operation_key -> id вопроса` в
    `payload.ask_ops` поручения, в одной записи. Повтор с тем же ключом — тот же вопрос без новой строки.
    Поручения нет или не видно агенту — 404; закрыто — 409; ключ не 1–100 символов — 422. -> строка
    вопроса."""
    from . import owner_talk as talk_mod  # импорт внутри: разговор сам опирается на этот модуль

    if not isinstance(operation_key, str) or not operation_key.strip() or len(operation_key) > ASK_OP_KEY_MAX:
        raise OwnerError(422, f"operation_key: строка 1–{ASK_OP_KEY_MAX} символов")
    errand = _agent_errand(conn, errand_id, principal)
    ops = errand["_payload"].get("ask_ops")
    ops = {k: v for k, v in ops.items() if isinstance(v, int)} if isinstance(ops, dict) else {}
    known = ops.get(operation_key)
    if known is not None:
        row = talk_mod._get(conn, known)  # noqa: SLF001
        if row is not None:
            return row
    row = talk_mod.post(
        conn,
        author="agent",
        text=text,
        ref=errand["target"],
        ask=True,
        ask_ttl_s=ask_ttl_s,
        now_ms=now_ms,
        audit=audit,
    )
    ops.pop(operation_key, None)
    ops[operation_key] = row["id"]
    while len(ops) > ASK_OPS_MAX:
        ops.pop(next(iter(ops)))
    payload = {**errand["_payload"], "ask_ops": ops}
    conn.execute(
        "UPDATE main.owner_item SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), errand_id),
    )
    return row


def errand_tell(
    conn,
    errand_id: int,
    *,
    operation_key,
    text,
    principal,
    now_ms: int,
    audit: dict,
) -> dict:
    """Ф94-1 (A.2): агент сообщает владельцу по своему открытому поручению (без ожидания ответа) — реплика
    агента (`owner_talk.post`, `tell=true`, `ref` и ветка — как у вопроса по этому поручению) и
    `operation_key -> id` в `payload.tell_ops` поручения (отдельно от `ask_ops`), в одной записи. Повтор с тем
    же ключом — та же строка. Поручения нет или не видно агенту — 404; закрыто — 409; ключ не 1–100 символов,
    пустой текст — 422. Текст в аудит не идёт. -> строка."""
    from . import owner_talk as talk_mod  # импорт внутри: разговор сам опирается на этот модуль

    if not isinstance(operation_key, str) or not operation_key.strip() or len(operation_key) > ASK_OP_KEY_MAX:
        raise OwnerError(422, f"operation_key: строка 1–{ASK_OP_KEY_MAX} символов")
    errand = _agent_errand(conn, errand_id, principal)
    ops = errand["_payload"].get("tell_ops")
    ops = {k: v for k, v in ops.items() if isinstance(v, int)} if isinstance(ops, dict) else {}
    known = ops.get(operation_key)
    if known is not None:
        row = talk_mod._get(conn, known)  # noqa: SLF001
        if row is not None:
            return row
    row = talk_mod.post(
        conn,
        author="agent",
        text=text,
        ref=errand["target"],
        tell=True,
        now_ms=now_ms,
        audit=audit,
    )
    ops.pop(operation_key, None)
    ops[operation_key] = row["id"]
    while len(ops) > ASK_OPS_MAX:
        ops.pop(next(iter(ops)))
    payload = {**errand["_payload"], "tell_ops": ops}
    conn.execute(
        "UPDATE main.owner_item SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), errand_id),
    )
    return row


class HandsStale(OwnerError):
    """Ф96-1 (П8): нажатие кнопки рук со старым `hands_ts` — 409 `stale` и текущее поручение (`errand`): бот
    обновляет кнопки по нему."""

    def __init__(self, errand: dict) -> None:
        super().__init__(409, HANDS_STALE)
        self.errand = errand


def _hands_ref_ok(errand: dict, ref) -> bool:
    prev = errand["_payload"].get("prev_targets") or []
    return isinstance(ref, str) and (ref == errand["target"] or ref in prev)


def _hands_write(conn, errand: dict, payload: dict, *, hold_until: int) -> None:
    """Ф96-1 (П1, П4): запись рук — новый `seq` поручения и заявки «нужен человек» (бот и интерфейс узнают о
    смене); срок заявки не раньше `hold_until` (руки у владельца — заявка не снимается по сроку)."""
    conn.execute(
        "UPDATE main.owner_item SET payload_json = ?, seq = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), next_seq(conn), errand["id"]),
    )
    m = _REF_RE.match(errand["target"]) if isinstance(errand["target"], str) else None
    if m is not None and m.group(1) == "human":
        human_mod.hold_until(conn, int(m.group(2)), hold_until)
    _bump_human(conn, errand["target"])


def errand_hands(
    conn,
    errand_id: int,
    to,
    *,
    by: str,
    principal,
    now_ms: int,
    audit: dict,
    note=None,
    reply_to=None,
    tg_message_id=None,
    hands_s=None,
) -> dict:
    """Ф96-1 (A.1, П1–П4, П8): владелец (кука, `by='ui'`) или его бот (`by='bot'`) забирает руки посреди
    поручения агента (`to='owner_channel'`) или отдаёт их обратно (`to='agent'`, `note` — что сделал, до 2000,
    маскируется; пусто — «Владелец вернул управление»). Поручение остаётся открытым. Возврат агенту — срок
    зависания с нуля (`progress_ts`). Уже так — без изменений (`changed: false`).

    Бот: `reply_to` — ответ владельца свайпом на реплику агента по этому поручению (П2): при возврате агенту его
    текст (`note`) пишется ещё и репликой владельца в ленту «Мой агент» (`via='bot'`) в той же записи;
    `tg_message_id` — повтор того же ответа без второй реплики (`repeat: true`). `hands_s` — секунды
    `hands_ts` с кнопки (0 — руки не передавались): не совпали — `HandsStale` (409 `stale` + поручение).

    Закрытое поручение — 409; нет или не видно — 404; неверное тело — 422.
    -> `{errand, changed[, repeat]}`."""
    from . import owner_talk as talk_mod  # импорт внутри: разговор сам опирается на этот модуль

    if by not in ("ui", "bot"):
        raise ValueError(f"by: {by!r}")
    if to not in HANDS:
        raise OwnerError(422, "to: owner_channel или agent")
    if note is not None and (not isinstance(note, str) or len(note) > HANDS_NOTE_MAX):
        raise OwnerError(422, f"note: не длиннее {HANDS_NOTE_MAX}")
    if to == "owner_channel" and (note is not None or reply_to is not None):
        raise OwnerError(422, "note, reply_to: только при возврате рук агенту")
    if reply_to is not None and by != "bot":
        raise OwnerError(422, "reply_to: только из бота")
    if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)):
        raise OwnerError(422, "reply_to: номер реплики агента")
    if reply_to is not None and (note is None or not note.strip()):
        raise OwnerError(422, "note: текст ответа владельца")
    if tg_message_id is not None and reply_to is None:
        raise OwnerError(422, "tg_message_id: только вместе с reply_to")
    if hands_s is not None and (isinstance(hands_s, bool) or not isinstance(hands_s, int) or hands_s < 0):
        raise OwnerError(422, "hands_s: целое ≥ 0")
    errand = _get_errand(conn, errand_id)
    if not _visible(principal, errand["project_id"]):
        raise OwnerError(404, "not found")
    if errand["closed_ts"] is not None:
        raise OwnerError(409, "поручение уже закрыто")
    if reply_to is not None:
        question = talk_mod._get(conn, reply_to)  # noqa: SLF001
        if question is None or question["author"] != "agent" or not _hands_ref_ok(errand, question["ref"]):
            raise OwnerError(422, "reply_to: не реплика агента по этому поручению")
        if tg_message_id is not None and talk_mod.bot_note_of(conn, reply_to, tg_message_id) is not None:
            return {"errand": _errand_public(errand), "changed": False, "repeat": True}
    if hands_s is not None and (errand["hands_ts"] or 0) // 1000 != hands_s:
        raise HandsStale(_errand_public(errand))
    if errand["hands"] == to:
        return {"errand": _errand_public(errand), "changed": False}
    payload = {**errand["_payload"], "hands": to, "hands_ts": now_ms, "hands_by": by}
    if to == "owner_channel":
        payload["hands_why"] = None
        hold = now_ms + HANDS_MAX_MS
    else:
        said = note.strip() if isinstance(note, str) else ""
        payload["hands_note"] = _mask(said) if said else HANDS_NOTE_DEFAULT
        payload["progress_ts"] = now_ms
        hold = now_ms + HANDS_BACK_MS
        if reply_to is not None:
            talk_mod.hands_reply(
                conn, reply_to=reply_to, text=said, tg_message_id=tg_message_id, now_ms=now_ms, audit=audit
            )
    _hands_write(conn, errand, payload, hold_until=hold)
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_errand",
        result=f"hands_{to}",
        detail={"errand_id": errand_id, "ref": errand["target"], "by": by},
        **audit,
    )
    return {"errand": _errand_public(_get_errand(conn, errand_id)), "changed": True}


def errand_handoff(
    conn,
    errand_id: int,
    *,
    operation_key,
    why,
    principal,
    now_ms: int,
    audit: dict,
) -> dict:
    """Ф96-1 (A.2, П4, П7): агент сам отдаёт руки владельцу — застрял. Руки у владельца (`hands_by='agent'`,
    `hands_why` — что не вышло и что сделать, до 500, маскируется) и реплика агента `tell` «Передаю тебе:
    <why>» в ленту «Мой агент» (`ref` и ветка — как у `errand_tell`; бот её доставит) — одной записью.
    Повтор с известным `operation_key` (ключи общие с `errand_tell`) — та же реплика, руки не трогаются.
    Нет поручения — 404; закрыто — 409; ключ не 1–100, пустой `why` или длиннее 500 — 422.
    -> `{errand, talk[, repeat]}`."""
    from . import owner_talk as talk_mod  # импорт внутри: разговор сам опирается на этот модуль

    if not isinstance(operation_key, str) or not operation_key.strip() or len(operation_key) > ASK_OP_KEY_MAX:
        raise OwnerError(422, f"operation_key: строка 1–{ASK_OP_KEY_MAX} символов")
    if not isinstance(why, str) or not why.strip() or len(why) > HANDS_WHY_MAX:
        raise OwnerError(422, f"why: 1–{HANDS_WHY_MAX} символов")
    errand = _agent_errand(conn, errand_id, principal)
    payload = errand["_payload"]
    ops = payload.get("tell_ops")
    ops = {k: v for k, v in ops.items() if isinstance(v, int)} if isinstance(ops, dict) else {}
    known = ops.get(operation_key)
    if known is not None:
        row = talk_mod._get(conn, known)  # noqa: SLF001
        if row is not None:
            return {"errand": _errand_public(errand), "talk": row, "repeat": True}
    why_masked = _mask(why.strip())
    row = talk_mod.post(
        conn,
        author="agent",
        text=HANDOFF_PREFIX + why_masked,
        ref=errand["target"],
        tell=True,
        now_ms=now_ms,
        audit=audit,
    )
    ops.pop(operation_key, None)
    ops[operation_key] = row["id"]
    while len(ops) > ASK_OPS_MAX:
        ops.pop(next(iter(ops)))
    handoffs = [i for i in payload.get("handoff_talk") or [] if isinstance(i, int)][-(HANDOFF_KEEP - 1) :]
    payload = {
        **payload,
        "tell_ops": ops,
        "handoff_talk": [*handoffs, row["id"]],
        "hands": "owner_channel",
        "hands_ts": now_ms,
        "hands_by": "agent",
        "hands_why": why_masked,
    }
    _hands_write(conn, errand, payload, hold_until=now_ms + HANDS_MAX_MS)
    write_audit(
        conn,
        now_ms=now_ms,
        action="owner_errand",
        result="handoff",
        detail={"errand_id": errand_id, "ref": errand["target"], "talk_id": row["id"]},
        **audit,
    )
    return {"errand": _errand_public(_get_errand(conn, errand_id)), "talk": row}


def errand_marks(conn, refs) -> dict[str, dict]:
    """Ф96-1 (B.1, П2, П6): реплики разговора по карточкам `refs` -> открытое поручение по каждой (и
    переехавшее с неё, `prev_targets`): `{ref: {errand_id, hands, hands_ts, handoff_ids}}`; нет поручения —
    ключа нет. Для `asks`/`tells` бота."""
    errands = _open_errands(conn)
    out: dict[str, dict] = {}
    for ref in refs:
        errand = errands.get(ref) or next(
            (e for e in errands.values() if ref in (e["_payload"].get("prev_targets") or [])), None
        )
        if errand is None:
            continue
        out[ref] = {
            "errand_id": errand["id"],
            "hands": errand["hands"],
            "hands_ts": errand["hands_ts"],
            "handoff_ids": {i for i in errand["_payload"].get("handoff_talk") or [] if isinstance(i, int)},
        }
    return out


def errand_state(
    conn,
    errand_id: int,
    principal,
    *,
    now_ms: int,
    presence: list[dict] | None = None,
    target: str | None = None,
) -> dict:
    """Ф85-1 (F): состояние поручения агента (и закрытого — для уборки после рестарта) -> `{id, state,
    can_act, target_ref, closed_by, closed_ts, result, returned}`. `can_act` — поручение открыто, его целевая
    карточка ещё открыта и (если агент назвал `target`) цель не переехала. Не видно агенту — 404.

    Ф96-1 (A.3, контракт D): ещё `hands` (`agent` | `owner_channel`), `hands_by`, `hands_ts`, `hands_why`,
    `hands_note` — руки у владельца: агент окно не трогает и ждёт; `can_act` от рук не зависит."""
    errand = _get_errand(conn, errand_id)
    if not _visible(principal, errand["project_id"]):
        raise OwnerError(404, "not found")
    is_open = errand["closed_ts"] is None
    can_act = False
    if is_open and (target is None or target == errand["target"]):
        open_refs = {c["ref"] for c in _collect(conn, principal, now_ms=now_ms, presence=presence or [])}
        can_act = errand["target"] in open_refs
    return {
        "id": errand["id"],
        "state": "open" if is_open else "closed",
        "can_act": can_act,
        "target_ref": errand["target"],
        "closed_by": errand["closed_by"],
        "closed_ts": errand["closed_ts"],
        "result": errand["result"] if isinstance(errand["result"], str) else None,
        "returned": errand["returned"] if isinstance(errand["returned"], str) else None,
        **{k: errand[k] for k in (*_HANDS_PUBLIC, "hands_note")},
    }


def close_errands(conn, errand_ids: list[int], *, now_ms: int) -> int:
    """Закрыть поручения `system` (их карточка закрыта). Уже закрытые пропускаются."""
    done = 0
    for errand_id in errand_ids:
        try:
            errand = _get_errand(conn, errand_id)
        except OwnerError:
            continue
        if errand["closed_ts"] is None:
            _close_errand(conn, errand, "system", now_ms=now_ms)
            done += 1
    return done


def _hung(conn, now_ms: int) -> list[tuple[dict, int]]:
    """Ф65-1 (Ж2): открытые поручения без шага дольше `HUNG_MS` -> `[(поручение, минут без шага)]`. Открытый
    вопрос агента владельцу по этой карточке (`owner_talk`, срок не вышел; Ф85-1: бессрочный — открыт всегда) —
    не зависание. Ф85-1: ответ владельца на вопрос агента по карточке — тоже шаг (агенту есть время его взять).

    Ф96-1 (A.4): руки у владельца — не зависание, пока им меньше `HANDS_MAX_MS` (считается от `hands_ts`); дольше —
    зависло (причину пишет `return_hung`). Шаги и вопросы агента такое поручение не держат."""
    from . import owner_talk as talk_mod  # импорт внутри: разговор сам опирается на этот модуль

    found: dict[int, int] = {}  # номер поручения -> минут без шага (порядок — порядок открытых)
    errands = list(_open_errands(conn).values())
    plain = []
    for errand in errands:
        if errand["hands"] == "owner_channel":
            since = errand["hands_ts"] if errand["hands_ts"] is not None else errand["progress_ts"]
            if now_ms - since >= HANDS_MAX_MS:
                found[errand["id"]] = max(0, now_ms - since) // 60_000
        elif now_ms - errand["progress_ts"] >= HUNG_MS:
            plain.append(errand)
    if plain:
        asking = talk_mod.open_asks(conn, now_ms=now_ms)
        answered = _answered_ts(conn, [e["target"] for e in plain if e["target"] not in asking])
        for errand in plain:
            if errand["target"] in asking:
                continue
            last = max(errand["progress_ts"], answered.get(errand["target"], 0))
            if now_ms - last >= HUNG_MS:
                found[errand["id"]] = max(0, now_ms - last) // 60_000
    return [(e, found[e["id"]]) for e in errands if e["id"] in found]


def _answered_ts(conn, targets: list[str]) -> dict[str, int]:
    """Ф85-1: карточка -> время последнего ответа владельца на вопрос агента по ней (`owner_talk`)."""
    if not targets:
        return {}
    marks = ",".join("?" for _ in targets)
    rows = conn.execute(
        "SELECT q.ref, MAX(a.ts) FROM main.owner_talk a JOIN main.owner_talk q ON a.reply_to = q.id "  # noqa: S608
        f"WHERE a.author = 'owner_channel' AND q.author = 'agent' AND q.ask = 1 AND q.ref IN ({marks}) "
        "GROUP BY q.ref",
        targets,
    ).fetchall()
    return {ref: ts for ref, ts in rows if isinstance(ts, int)}


def hung_errands(conn, *, now_ms: int) -> list[int]:
    """Читатель (Ж2): номера зависших поручений — их возвращает `return_hung` отдельной записью."""
    return [e["id"] for e, _ in _hung(conn, now_ms)]


def return_hung(conn, *, now_ms: int) -> list[int]:
    """Ф65-1 (Ж2): зависшие поручения — владельцу: `closed_by='system'`, причина «завис: последний шаг «…» N мин
    назад». Зовут минутный цикл ядра и чтение очереди. Возвращает номера закрытых."""
    done = []
    for errand, minutes in _hung(conn, now_ms):
        step = errand["progress"]
        if errand["hands"] == "owner_channel":
            reason = HANDS_HUNG  # Ф96-1 (A.4)
        elif isinstance(step, str) and step:
            reason = f"завис: последний шаг «{_clip_text(step, 120)}» {minutes} мин назад"
        else:
            reason = f"завис: ни одного шага {minutes} мин"
        payload = {**errand["_payload"], "returned": reason, "returned_detail": None}
        _close_errand(conn, errand, "system", now_ms=now_ms, payload=payload)
        write_audit(
            conn,
            now_ms=now_ms,
            actor="core",
            api_token_id=None,
            client_name=None,
            request_id="",
            action="owner_errand",
            result="hung",
            detail={
                "errand_id": errand["id"],
                "ref": errand["target"],
                "minutes": minutes,
                **({"hands": "owner_channel"} if errand["hands"] == "owner_channel" else {}),
            },
        )
        done.append(errand["id"])
    return done


# --- закрытое (только владельцу, П8) -----------------------------------------------------------------------------


def closed(conn, *, limit: int = 50) -> list[dict]:
    """Последние закрытые строки `owner_item` с источником (`closed_by`, `closed_token_id`) — видно только
    владельцу. Ф65-1 (Ж4): `errand_result` — итог агента (у поручения и у строки ответа агента), иначе `None`;
    Ф76-3: `basis` — основание его ответа, иначе `None`. Ф92-1 (A2): `agent_reply` — у карточки `turn`,
    закрытой агентом, `{"action": "reply"|"ok"|"deny", "text": <реплика, до 300 знаков, маска>|None}`."""
    limit = max(1, min(int(limit), CLOSED_MAX))
    rows = conn.execute(
        "SELECT id, ref, kind, project_id, session_id, created_ts, closed_ts, closed_by, closed_token_id, "
        "payload_json FROM main.owner_item WHERE closed_ts IS NOT NULL "
        "ORDER BY closed_ts DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row[9]) if row[9] else None
        except ValueError:
            payload = None
        agent_reply = None
        if row[2] == "turn" and row[7] == "agent" and isinstance(payload, dict):
            act = payload.get("action")
            if act in ("reply", "ok", "deny"):
                said = decisions_mod.say_text(conn, payload.get("say_id")) if act == "reply" else None
                agent_reply = {"action": act, "text": _brief(said)[:300] if said else None}
        out.append(
            {
                "id": row[0],
                "ref": row[1],
                "kind": row[2],
                "project_id": row[3],
                "session_id": row[4],
                "created_ts": row[5],
                "closed_ts": row[6],
                "closed_by": row[7],
                "closed_token_id": row[8],
                "payload": _mask_tree(payload) if isinstance(payload, dict) else None,
                "agent_reply": agent_reply,
                "errand_result": (
                    payload["result"]
                    if isinstance(payload, dict) and isinstance(payload.get("result"), str)
                    else None
                ),
                # Ф76-3: основание ответа агента (у поручения и у строки ответа агента), иначе `None`.
                "basis": (
                    payload["basis"]
                    if isinstance(payload, dict) and isinstance(payload.get("basis"), str)
                    else None
                ),
            }
        )
    return out


# --- перевод отказов в коды ---------------------------------------------------------------------------------

_BY_NAME = {
    "Invalid": 422,
    "Denied": 403,
    "NotFound": 404,
    "Conflict": 409,
    "TooMany": 429,
    "Limited": 429,
    "StaleLease": 409,
}


def error_of(exc: BaseException) -> tuple[int, str] | None:
    """Отказ модуля ядра -> (код, текст), как его отдают маршруты интерфейса; чужое исключение — `None`."""
    if isinstance(exc, OwnerError):
        return exc.status, exc.detail
    if isinstance(exc, peer_msgs_mod.PeerError):
        return exc.status, str(exc.detail)
    if isinstance(
        exc,
        SessionError
        | agent_requests_mod.AgentRequestError
        | reminders_mod.ReminderError
        | card_route_mod.RouteError,
    ):
        return exc.status, str(exc.detail)
    if isinstance(exc, queries.NotFound | outbox_mod.NotFound):
        return 404, "not found"
    if isinstance(exc, outbox_mod.SendAtPassed):
        return 409, "send_at_passed: время отправки прошло — ответь с send_now"
    if isinstance(exc, outbox_mod.NotOwnDraft):
        return 403, "forbidden"
    if isinstance(exc, outbox_mod.InvalidTransition):
        return 409, str(exc) or "invalid transition"
    if isinstance(exc, outbox_mod.OutboxError):
        return 422, str(exc)
    if isinstance(
        exc,
        human_mod.HumanError
        | presses_mod.PressError
        | join_mod.JoinError
        | leave_mod.LeaveError
        | group_ops_mod.GroupOpError
        | rule_requests_mod.RuleRequestError,
    ):
        return _BY_NAME.get(type(exc).__name__, 422), str(exc)
    return None
