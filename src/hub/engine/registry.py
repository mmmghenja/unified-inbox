"""Профиль движка сессий хаба (Ф74-1, docs/architecture.md): одна запись на движок —
подпись, исполняемые файлы, свойства хода, усилие и подсказки модели, ключи настроек хоста. Списки движков
сессий хаба, подписи, `MID_ENGINES`, `ENGINE_BINARIES`, `NESTED_AGENTS`, потолки и выключатели хоста берутся
отсюда; `/status` отдаёт профили интерфейсу.

Не сюда (В5): значения глобального переключателя wake/work/orch (`core/engine.py` `ENGINES`) и движок
`hub-facts` (`facts/service.py` `ENGINES`) — это другие списки, они не сливаются с профилем.

`available=False` — заготовка: сессии хаба на таком движке не создаются, интерфейс его не предлагает, его
исполняемые файлы не входят ни в уборку сирот хоста, ни в запрет вложенного запуска гарда (поведение не
меняется, пока движок не включён кодом). Модуль без зависимостей — его берут ядро, хост, гард и MCP-сервер."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EngineProfile:
    name: str  # значение `hub_session.engine`
    label: str  # подпись для людей и моделей: «Claude Code»
    short: str  # короткая подпись (чип на телефоне)
    binaries: tuple[str, ...]  # имена исполняемых файлов: уборка сирот хоста и запрет вложенного запуска
    mid_turn: bool  # реплики и сигналы подаются посреди хода (Ф55-1, Ф50-1)
    host_decides: bool  # каждый вызов инструмента решает хост (гард хаба), а не CLI движка
    compacts_self: bool  # движок сжимает разговор сам — сжатия хаба нет
    context_known: bool  # движок сообщает размер контекста (`usage.context_limit`)
    efforts: tuple[str, ...]  # уровни «Усилия» в интерфейсе
    model_hints: tuple[str, ...]  # подсказки поля «модель» (список не закрытый)
    enabled_key: str | None  # выключатель движка в настройках хоста (`None` — всегда включён)
    max_parallel_key: str | None  # свой потолок живых процессов (`None` — только общий `held_max_parallel`)
    available: bool = True  # сессии хаба на нём можно создавать
    # Ф74-5: системный текст закрепляется при создании разговора — продолжение нового не применяет, поправка
    # первым блоком подачи (Ф48-5, снимок `sys_pin`).
    pins_system: bool = False
    # Ф74-5 (спека, 3): выключатели «Автопилота», которые на движке не действуют, — интерфейс их
    # не показывает.
    dead_switches: tuple[str, ...] = ()
    # Ф74-2 (план 6.2 Б2): один процесс движка держит много разговоров (сервер OpenCode, процесс Codex, Cursor
    # CLI и IDE): другой разговор того же процесса — другая живая сессия, а не «сменившийся разговор»
    # (`/clear` у Claude Code).
    many_convs_per_proc: bool = False
    # Ф74-2 (В2): брошенная подписка внешней сессии такого движка — нет ожидания, последнее старше срока и
    # сама подписка старше срока (`core.subs.oc_stale`); `None` — живость только по процессу (Claude Code).
    idle_grace_ms: int | None = None
    # Ф74-2 (Б1): внешнее окно этого движка узнаётся по предку процесса MCP-сервера хаба — имена исполняемого
    # файла (`argv[0]`, `/proc/<pid>/exe`, `comm`); пусто — внешних окон через предка у движка нет. Личность
    # такой сессии — `session_id` вида `<движок>:<id разговора>` (как `ses_…` у OpenCode).
    ext_binaries: tuple[str, ...] = ()
    # Ф74-2: установщик плагина хаба для внешних окон движка (подсказка в отказе «нужен плагин»).
    ext_installer: str | None = None
    # Ф74-8 (матрица паритета, план 7): чего у движка нет — профиль говорит прямо, интерфейс и хост берут
    # отсюда. `asks_questions` — вопрос с вариантами карточкой владельцу (AskUserQuestion, `question`,
    # `request_user_input`); `mid_turn_tool_only` — реплика посреди хода входит только с ближайшим вызовом
    # инструмента (нет вызова до конца хода — уходит следующим ходом); `mcp_statuses` — «Перечитать MCP»
    # знает состояние серверов; `cache_key` — срок тёплого кэша провайдера (с) в настройках хоста, от конца
    # последнего хода (`None` — свой источник срока или срока нет).
    asks_questions: bool = True
    mid_turn_tool_only: bool = False
    mcp_statuses: bool = True
    cache_key: str | None = None


PROFILES: tuple[EngineProfile, ...] = (
    EngineProfile(
        name="claude",
        label="Claude Code",
        short="Claude",
        binaries=("claude",),
        mid_turn=True,
        host_decides=False,
        compacts_self=False,
        context_known=True,
        efforts=("low", "medium", "high", "xhigh", "max"),
        model_hints=("sonnet", "opus", "haiku"),
        enabled_key=None,
        max_parallel_key=None,
        pins_system=True,
    ),
    EngineProfile(
        name="opencode",
        label="OpenCode",
        short="OpenCode",
        binaries=("opencode",),
        mid_turn=False,
        host_decides=True,
        compacts_self=False,
        context_known=True,
        efforts=("low", "high", "max"),
        # Владелец 22.10: Anthropic подключён в OpenCode на обеих машинах (API организации) — Sonnet 5.5 в списке.
        model_hints=("deepseek/deepseek-flash", "anthropic/claude-sonnet-5-5"),
        enabled_key="held_opencode",
        max_parallel_key="held_max_parallel_opencode",
        many_convs_per_proc=True,
        idle_grace_ms=10 * 60 * 1000,  # Ф16-4 П1
    ),
    EngineProfile(
        name="cursor",
        label="Cursor",
        short="Cursor",
        binaries=("cursor-agent", "agent"),  # `agent` — второе имя того же CLI
        mid_turn=True,
        host_decides=True,
        compacts_self=True,
        context_known=False,
        efforts=("high", "xhigh"),  # не ниже high (слово владельца, Ф71-1 П11)
        model_hints=("grok-4.7", "claude-sonnet-5-5", "claude-opus-5-5"),
        enabled_key="held_cursor",
        max_parallel_key="held_max_parallel_cursor",
        # Проба 21.10 (cursor-agent 2026.10.19): MCP-сервер — прямой потомок процесса `cursor-agent`
        # (`argv[0]` `…/cursor-agent`, `exe` — свой `node`, `comm` — `MainThread`); IDE держит много чатов в
        # одном процессе. Одно значение на CLI и IDE: разговоры одного процесса не судят друг друга.
        many_convs_per_proc=True,
        # Ожидание идёт только в хуке `stop` между ходами: ход без вызовов хаба дольше 10 мин подписку не
        # снимает (В2), брошенный чат IDE держит подписку до 6 ч.
        idle_grace_ms=6 * 60 * 60 * 1000,
        ext_binaries=("cursor-agent", "agent", "cursor", "Cursor"),
        ext_installer="scripts/cursor-hub-install.sh",
        # Ф74-8, пробы `.runs/f748-probe/` (cursor-agent 2026.10.19): в `-p` инструмента вопроса с
        # вариантами нет (модель его не видит); `SwitchMode` хуков не проходит и одобряется самим Cursor
        # (режим живёт до конца хода — следующий процесс снова `agent`), `SendMessage` нет — выключатели
        # `plan_mode` и `cross_session` ничего бы не меняли; `mcp list` серверов плагина не видит, в потоке
        # состояния серверов нет; реплика посреди хода входит с ближайшим `preToolUse`.
        dead_switches=("cross_session", "plan_mode"),
        asks_questions=False,
        mid_turn_tool_only=True,
        mcp_statuses=False,
        cache_key="held_cache_s_cursor",
    ),
    # Ф74-5: app-server, гард хуком `PreToolUse`, `turn/steer` посреди хода (план, 6.1–6.2). Модели и усилия
    # проверяются по `model/list` движка при запуске — здесь только подсказки интерфейсу.
    EngineProfile(
        name="codex",
        label="Codex",
        short="Codex",
        binaries=("codex",),
        mid_turn=True,
        host_decides=True,
        compacts_self=False,
        context_known=True,
        # Подсказки; допустимые — `model/list` (живой список — поле `models` в `/status`, Ф74-5б).
        efforts=("low", "medium", "high", "xhigh", "max", "ultra"),
        model_hints=("gpt-6.1-sol",),
        enabled_key="held_codex",
        max_parallel_key="held_max_parallel_codex",
        pins_system=True,
        cache_key="held_cache_s_codex",
        # `SendMessage` и режима плана у Codex нет: галочки ничего бы не меняли.
        dead_switches=("cross_session", "plan_mode"),
        # Внешний Codex: один процесс (TUI или общий демон) — много нитей; ожидатель хука живёт отдельно от
        # хода и будит нить `codex queue` (проба 21.10, спека 74-7), поэтому срок брошенной — как у OpenCode.
        many_convs_per_proc=True,
        idle_grace_ms=10 * 60 * 1000,
        ext_binaries=("codex",),
        ext_installer="scripts/codex-hub-install.sh",
    ),
)

BY_NAME: dict[str, EngineProfile] = {p.name: p for p in PROFILES}
# Движки сессий хаба (порядок — как в интерфейсе; первый — умолчание).
HELD_ENGINES: tuple[str, ...] = tuple(p.name for p in PROFILES if p.available)
# Подписи всех известных движков (в том числе заготовок — подпись безвредна).
LABELS: dict[str, str] = {p.name: p.label for p in PROFILES}
# Движки, которым реплики и сигналы подаются посреди хода.
MID_ENGINES: tuple[str, ...] = tuple(p.name for p in PROFILES if p.available and p.mid_turn)
# Исполняемые файлы движков сессий хаба (только включённых).
BINARIES: tuple[str, ...] = tuple(b for p in PROFILES if p.available for b in p.binaries)
# Ф74-2: движки внешних окон, узнаваемых по предку процесса (Codex, Cursor).
EXTERNAL: tuple[str, ...] = tuple(p.name for p in PROFILES if p.ext_binaries)
EXT_SEP = ":"  # `session_id` внешней сессии: `<движок>:<id разговора>`


def get(name: str | None) -> EngineProfile | None:
    return BY_NAME.get(name) if isinstance(name, str) else None


def held(name: str | None) -> EngineProfile | None:
    """Профиль движка, на котором можно создать сессию хаба; иначе `None`."""
    profile = get(name)
    return profile if profile is not None and profile.available else None


def ext_engine_of(session_id: str | None) -> str | None:
    """Движок внешней сессии по приставке `session_id` (`codex:…`, `cursor:…`); иначе `None`."""
    if not isinstance(session_id, str):
        return None
    head, sep, rest = session_id.partition(EXT_SEP)
    return head if sep and rest and head in EXTERNAL else None


def ext_engine_by_binary(names) -> str | None:
    """Движок внешнего окна по именам процесса (`argv[0]`, `exe`, `comm` — базовые имена); иначе `None`."""
    found = set(names)
    for p in PROFILES:
        if p.ext_binaries and found.intersection(p.ext_binaries):
            return p.name
    return None


def label(name: str | None, default: str | None = None) -> str:
    """Подпись движка; неизвестный — `default`, а без него — само имя."""
    profile = get(name)
    if profile is not None:
        return profile.label
    return default if default is not None else str(name)


def check_held(value: str) -> str:
    """Проверка поля `engine` тел ручек (вместо `Literal`): только движки сессий хаба."""
    if held(value) is None:
        raise ValueError(f"engine: {' или '.join(HELD_ENGINES)}")
    return value


def status_profiles(models: dict[str, list[dict]] | None = None) -> list[dict]:
    """`/status`: профили движков сессий хаба для интерфейса (заготовки — с `available: false`). `models` —
    живые списки моделей по движкам (Ф74-5б, `core/engine_models.py`): непустой — полем `models` профиля."""
    out = [
        {
            "name": p.name,
            "label": p.label,
            "short": p.short,
            "efforts": list(p.efforts),
            "model_hints": list(p.model_hints),
            "compacts_self": p.compacts_self,
            "context_known": p.context_known,
            "mid_turn": p.mid_turn,
            "available": p.available,
            "dead_switches": list(p.dead_switches),
            "asks_questions": p.asks_questions,
            "mid_turn_tool_only": p.mid_turn_tool_only,
            "mcp_statuses": p.mcp_statuses,
        }
        for p in PROFILES
    ]
    for entry in out:
        listed = (models or {}).get(entry["name"])
        if listed:
            entry["models"] = listed
    return out
