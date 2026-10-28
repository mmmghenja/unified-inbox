"""Раскладка каналов по проектам. Спецификация: docs/architecture.md

Вызывается ТОЛЬКО внутри `Db.write()` (как и `ingest.py`/`outbox.py`/`channels.py`) — принимает уже открытое
`conn`, время параметром `now_ms`, схему (`main`/`work`) параметром `schema`. Правила аккаунта, канал и его
проект всегда лежат в ОДНОМ файле (правила ссылаются на `account_id`, каналы — тоже), поэтому вся раскладка
одного аккаунта не пересекает границу main/work.

`project_id` живёт в `main.project`; для work-аккаунтов правило может указывать на любой `project_id` —
проверка существования делает `_project_exists` через `main.project` независимо от `schema` канала (это уже
существующий приём в `channels.update_channel`).
"""

import json

from .db import next_seq


class MappingError(Exception):
    """Базовая ошибка раскладки."""


class InvalidMatch(MappingError):
    """`match` неизвестной формы -> 422 при создании правила."""


class NotFound(MappingError):
    """Аккаунт, правило или проект не найдены -> 404."""


class DuplicateRule(MappingError):
    """Ф14-1, решение 3: то же условие того же аккаунта в тот же проект уже есть -> 409."""

    def __init__(self, rule_id: int) -> None:
        super().__init__(f"такое правило уже есть (#{rule_id})")
        self.rule_id = rule_id


def _sql(schema: str, template: str, **parts: str) -> str:
    """Тот же приём, что в ingest.py/outbox.py/channels.py: схема и доверенные фрагменты — не пользовательский
    ввод, подставляются здесь, чтобы ruff (S608) не путал это с конкатенацией пользовательских данных."""
    return template.format(schema=schema, **parts)


_ALLOWED_MATCH_KEYS = (
    frozenset({"channel_ext_id"}),
    frozenset({"channel_ext_id", "label"}),
    frozenset({"attr", "has"}),
    frozenset({"attr", "has", "label"}),
    frozenset({"all"}),
    frozenset({"all", "label"}),
)

# Раздел G спеки 2b: приоритет по умолчанию для правила «весь аккаунт» — ОТРИЦАТЕЛЬНЫЙ, чтобы частные
# правила (приоритет 0 по умолчанию) и ручная привязка всегда побеждали над catch-all.
DEFAULT_ALL_PRIORITY = -100
DEFAULT_PRIORITY = 0


def validate_match(match: dict) -> None:
    """`match` — один из известных видов (раздел B; `{"all": true}` — раздел G), с необязательным `label`.
    Иное -> `InvalidMatch`."""
    if not isinstance(match, dict):
        raise InvalidMatch("match должен быть объектом")
    keys = frozenset(match)
    if keys not in _ALLOWED_MATCH_KEYS:
        raise InvalidMatch(f"match неизвестной формы: ключи {sorted(match)}")
    if "channel_ext_id" in match and not isinstance(match["channel_ext_id"], str):
        raise InvalidMatch("channel_ext_id должен быть строкой")
    if "attr" in match and not isinstance(match["attr"], str):
        raise InvalidMatch("attr должен быть строкой")
    if "label" in match and not isinstance(match["label"], str):
        raise InvalidMatch("label должен быть строкой")
    if "all" in match and match["all"] is not True:
        raise InvalidMatch("all должен быть true")


def default_priority(match: dict) -> int:
    """Раздел G: умолчание приоритета зависит от формы `match` — `{"all"}` весит МЕНЬШЕ обычных правил."""
    return DEFAULT_ALL_PRIORITY if "all" in match else DEFAULT_PRIORITY


def _match_rule(match: dict, *, channel_ext_id: str, attrs: dict | None) -> bool:
    if "all" in match:
        return True
    if "channel_ext_id" in match:
        return match["channel_ext_id"] == channel_ext_id
    key = match["attr"]
    has = match["has"]
    if attrs is None or key not in attrs:
        return False
    value = attrs[key]
    if isinstance(value, list):
        return has in value
    return value == has


def _describe_match(match: dict) -> str:
    label = match.get("label")
    if label:
        return label
    if "all" in match:
        return "весь аккаунт"
    if "channel_ext_id" in match:
        return f"channel_ext_id={match['channel_ext_id']}"
    return f"{match['attr']}∋{match['has']!r}"


def _channel_attrs(meta_json: str | None) -> dict | None:
    if not meta_json:
        return None
    try:
        meta = json.loads(meta_json)
    except (ValueError, TypeError):
        return None
    attrs = meta.get("attrs") if isinstance(meta, dict) else None
    return attrs if isinstance(attrs, dict) else None


def _account_rules(conn, account_id: int) -> list[tuple[int, dict, int]]:
    """Правила аккаунта, отсортированные `priority DESC, id ASC` (раздел B, «Порядок»)."""
    rows = conn.execute(
        "SELECT id, match_json, project_id FROM main.map_rule WHERE account_id = ? "
        "ORDER BY priority DESC, id ASC",
        (account_id,),
    ).fetchall()
    return [(rid, json.loads(match_json), project_id) for rid, match_json, project_id in rows]


def _choose_auto(
    rules: list[tuple[int, dict, int]],
    *,
    ext_id: str,
    attrs: dict | None,
    is_topic: bool,
    parent_project_id: int | None,
) -> tuple[int | None, str]:
    """Чистый выбор проекта АВТО-канала (Ф14-1 A3): один резолвер на пересчёт и предпросмотр. Топик следует
    за проектом форума (`parent_project_id` — параметром: пересчёт берёт его из БД, предпросмотр — из
    своего расчёта), остальные — первое подошедшее правило из `rules` (порядок `priority DESC, id ASC`)."""
    if is_topic:
        return parent_project_id, "как у форума"
    for rule_id, match, project_id in rules:
        if _match_rule(match, channel_ext_id=ext_id, attrs=attrs):
            return project_id, f"правило #{rule_id}: {_describe_match(match)}"
    return None, "нет правил"


def _resolve_auto(conn, schema: str, channel_id: int) -> tuple[int | None, str]:
    """`(project_id, map_reason)` для АВТО-канала (топик следует за форумом, остальные — по правилам)."""
    row = conn.execute(
        _sql(
            schema,
            "SELECT account_id, ext_id, kind, parent_channel_id, meta_json FROM {schema}.channel "
            "WHERE id = ?",
        ),
        (channel_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"канал {channel_id} не найден")
    account_id, ext_id, kind, parent_channel_id, meta_json = row

    is_topic = kind == "topic" and parent_channel_id is not None
    parent_project_id = None
    rules: list[tuple[int, dict, int]] = []
    if is_topic:
        parent = conn.execute(
            _sql(schema, "SELECT project_id FROM {schema}.channel WHERE id = ?"), (parent_channel_id,)
        ).fetchone()
        parent_project_id = parent[0] if parent is not None else None
    else:
        rules = _account_rules(conn, account_id)
    return _choose_auto(
        rules,
        ext_id=ext_id,
        attrs=_channel_attrs(meta_json),
        is_topic=is_topic,
        parent_project_id=parent_project_id,
    )


def resolve_auto_project(conn, schema: str, channel_id: int) -> int | None:
    """Проект, который канал получил бы по правилам (как `map_source='auto'`), БЕЗ записи — тем же
    резолвером, что пересчёт. Спека 7a, П4: проверка «перенос не расширяет видимость» для `map_source:
    "auto"` от bearer."""
    project_id, _reason = _resolve_auto(conn, schema, channel_id)
    return project_id


def recompute_channel(conn, schema: str, channel_id: int, now_ms: int) -> bool:
    """Пересчитать один канал, если он `map_source='auto'`. `manual` — никогда не трогается (раздел B).
    Возвращает True, если `project_id`/`map_reason` реально изменились (тогда `channel.seq` вырос)."""
    del now_ms  # само значение не хранится — параметр ради единообразия сигнатур core
    row = conn.execute(
        _sql(schema, "SELECT map_source, project_id, map_reason FROM {schema}.channel WHERE id = ?"),
        (channel_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"канал {channel_id} не найден")
    map_source, old_project_id, old_map_reason = row
    if map_source != "auto":
        return False

    new_project_id, new_map_reason = _resolve_auto(conn, schema, channel_id)
    if new_project_id == old_project_id and new_map_reason == old_map_reason:
        return False

    seq = next_seq(conn)
    conn.execute(
        _sql(
            schema,
            "UPDATE {schema}.channel SET project_id = ?, map_reason = ?, seq = ? WHERE id = ?",
        ),
        (new_project_id, new_map_reason, seq, channel_id),
    )
    return True


def recompute_topics(conn, schema: str, parent_channel_id: int, now_ms: int) -> int:
    """Пересчитать авто-топики этого канала (после смены его проекта, руками или правилом). Возвращает
    число реально изменившихся топиков."""
    rows = conn.execute(
        _sql(
            schema,
            "SELECT id FROM {schema}.channel WHERE parent_channel_id = ? AND kind = 'topic' "
            "AND map_source = 'auto'",
        ),
        (parent_channel_id,),
    ).fetchall()
    changed = 0
    for (topic_id,) in rows:
        if recompute_channel(conn, schema, topic_id, now_ms):
            changed += 1
    return changed


def recompute_channel_cascade(conn, schema: str, channel_id: int, now_ms: int) -> int:
    """Пересчитать канал и (если он форум) его авто-топики. Возвращает число изменившихся каналов (0-N)."""
    changed = 1 if recompute_channel(conn, schema, channel_id, now_ms) else 0
    changed += recompute_topics(conn, schema, channel_id, now_ms)
    return changed


def recompute_account(conn, schema: str, account_id: int, now_ms: int) -> int:
    """Пересчитать ВСЕ авто-каналы аккаунта (добавление/удаление правила, раздел B). Не-топики — первым
    проходом (правила), топики — вторым (следуют за уже пересчитанными форумами)."""
    non_topics = conn.execute(
        _sql(
            schema,
            "SELECT id FROM {schema}.channel WHERE account_id = ? AND map_source = 'auto' "
            "AND kind <> 'topic'",
        ),
        (account_id,),
    ).fetchall()
    changed = 0
    for (channel_id,) in non_topics:
        if recompute_channel(conn, schema, channel_id, now_ms):
            changed += 1

    topics = conn.execute(
        _sql(
            schema,
            "SELECT id FROM {schema}.channel WHERE account_id = ? AND map_source = 'auto' AND kind = 'topic'",
        ),
        (account_id,),
    ).fetchall()
    for (channel_id,) in topics:
        if recompute_channel(conn, schema, channel_id, now_ms):
            changed += 1
    return changed


# --- Админ-ручки: CRUD правил -------------------------------------------------------------------------------


def _account_schema(conn, account_id: int) -> str | None:
    row = conn.execute("SELECT db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    return row[0] if row is not None else None


def storable_match(conn, account_id: int, match: dict) -> dict:
    """`match` в том виде, в каком его можно положить в `main` (правило и audit живут там). У work-аккаунта
    `label` — название папки/канала корпоративного источника — в `main` попадать не должен (граница main/work,
    раздел A.4; находка 6 критика 1a): отбрасывается, `map_reason` покажет саму форму правила."""
    if _account_schema(conn, account_id) == "work" and "label" in match:
        return {k: v for k, v in match.items() if k != "label"}
    return match


def add_rule(
    conn, *, account_id: int, project_id: int, match: dict, priority: int | None, now_ms: int
) -> tuple[int, int]:
    """Создать правило. `priority=None` -> ядро решает (раздел G: `{"all"}` весит `DEFAULT_ALL_PRIORITY`,
    иначе `DEFAULT_PRIORITY`); явный приоритет уважается. Возвращает `(rule_id, число_переложенных_каналов)`.
    """
    validate_match(match)
    schema = _account_schema(conn, account_id)
    if schema is None:
        raise NotFound(f"account_id {account_id} не найден")
    match = storable_match(conn, account_id, match)
    exists = conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone()
    if exists is None:
        raise NotFound(f"project_id {project_id} не существует")
    if priority is None:
        priority = default_priority(match)

    cur = conn.execute(
        "INSERT INTO main.map_rule (account_id, match_json, project_id, priority) VALUES (?, ?, ?, ?)",
        (account_id, json.dumps(match, ensure_ascii=False, sort_keys=True), project_id, priority),
    )
    rule_id = cur.lastrowid
    changed = recompute_account(conn, schema, account_id, now_ms)
    return rule_id, changed


def remove_rule(conn, rule_id: int, now_ms: int) -> tuple[int, int]:
    """Удалить правило. Возвращает `(account_id, число_переложенных_каналов)`."""
    row = conn.execute("SELECT account_id FROM main.map_rule WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise NotFound(f"правило {rule_id} не найдено")
    account_id = row[0]
    schema = _account_schema(conn, account_id)
    conn.execute("DELETE FROM main.map_rule WHERE id = ?", (rule_id,))
    changed = recompute_account(conn, schema, account_id, now_ms) if schema is not None else 0
    return account_id, changed


def list_rules(conn, account_id: int | None) -> list[dict]:
    if account_id is not None:
        rows = conn.execute(
            "SELECT id, account_id, match_json, project_id, priority FROM main.map_rule "
            "WHERE account_id = ? ORDER BY priority DESC, id ASC",
            (account_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, account_id, match_json, project_id, priority FROM main.map_rule "
            "ORDER BY account_id, priority DESC, id ASC"
        ).fetchall()
    return [
        {
            "id": rid,
            "account_id": acc_id,
            "match": json.loads(match_json),
            "project_id": project_id,
            "priority": priority,
        }
        for rid, acc_id, match_json, project_id, priority in rows
    ]


# --- Ф14-1: условие правила собирает ядро, описание для людей, предпросмотр ---------------------------------
# docs/architecture.md, A1–A4. Всё ниже только читает — годится и для `db.reader()`, и внутри
# `db.write()`.

_LABEL_MAX = 200
_OPTIONS_LIMIT = 100
_EXAMPLES_LIMIT = 20


def _account_row(conn, account_id: int) -> tuple[str, str]:
    """`(source, schema)` аккаунта; нет -> `NotFound`."""
    row = conn.execute("SELECT source, db FROM main.account WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFound(f"account_id {account_id} не найден")
    return row[0], row[1]


def tg_folders(conn, schema: str, account_id: int) -> list[dict] | None:
    """`account_meta.tg_folders` из файла аккаунта (`[{id, title}]`) или `None`, если коннектор их ещё не
    прислал. Свой маленький запрос: `mapping` не импортирует `queries` (там есть `account_meta_value`)."""
    row = conn.execute(
        _sql(schema, "SELECT value_json FROM {schema}.account_meta WHERE account_id = ? AND key = ?"),
        (account_id, "tg_folders"),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except ValueError, TypeError:
        return None
    if not isinstance(value, list):
        return None
    return [f for f in value if isinstance(f, dict) and "id" in f]


def _folders_text(folders: list[dict]) -> str:
    return ", ".join(f"{f['id']}={f.get('title')}" for f in folders) or "(список пуст)"


def build_match(conn, account_id: int, kind: str, value) -> dict:
    """Ф14-1 A1 (П1): `match` правила из вида и значения — общий путь UI-ручки и админ-ручки (CLI).
    Ошибки — `InvalidMatch` с человеческим текстом (уходит в 422 как есть), аккаунта нет — `NotFound`."""
    source, schema = _account_row(conn, account_id)
    if kind == "folder":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidMatch("папка: нужен номер папки (целое число)")
        if source != "telegram":
            raise InvalidMatch("папки есть только у Telegram")
        folders = tg_folders(conn, schema, account_id)
        if folders is None:
            raise InvalidMatch("папки ещё не пришли от коннектора")
        found = next((f for f in folders if f["id"] == value), None)
        if found is None:
            raise InvalidMatch(f"папка {value} неизвестна. Известные папки: {_folders_text(folders)}")
        return {"attr": "tg_folders", "has": value, "label": f"папка «{found.get('title')}»"}
    if kind == "label":
        if not isinstance(value, str):
            raise InvalidMatch("метка: нужно название метки")
        label = value.strip()
        if not label or len(label) > _LABEL_MAX:
            raise InvalidMatch(f"метка: от 1 до {_LABEL_MAX} символов")
        if source != "mail":
            raise InvalidMatch("метки есть только у почты")
        return {"attr": "labels", "has": label, "label": f"метка «{label}»"}
    if kind == "domain":
        if not isinstance(value, str):
            raise InvalidMatch("отправитель: нужен домен вида example.com")
        domain = value.strip().lstrip("@").lower()
        if not domain or "@" in domain or any(ch.isspace() for ch in domain) or "." not in domain:
            raise InvalidMatch("отправитель: нужен домен вида example.com")
        if source != "mail":
            raise InvalidMatch("отправитель (домен) есть только у почты")
        return {"attr": "from_domains", "has": domain, "label": f"отправитель @{domain}"}
    if kind == "all":
        if value is not None:
            raise InvalidMatch("весь аккаунт: значение не нужно")
        return {"all": True}
    if kind == "channel":
        if not isinstance(value, str) or not value:
            raise InvalidMatch("беседа: нужен ext_id беседы (строка)")
        return {"channel_ext_id": value}
    raise InvalidMatch(f"неизвестный вид условия {kind!r}: folder, label, domain, all или channel")


def describe_rule(conn, account_id: int, match: dict) -> tuple[str, object, str]:
    """Ф14-1 A2: `(kind, value, text)` правила для людей. `map_reason` каналов этим НЕ меняется."""
    schema = _account_schema(conn, account_id)
    if "all" in match:
        return "all", None, "весь аккаунт"
    if "channel_ext_id" in match:
        ext_id = match["channel_ext_id"]
        title = None
        if schema is not None:
            row = conn.execute(
                _sql(schema, "SELECT title FROM {schema}.channel WHERE account_id = ? AND ext_id = ?"),
                (account_id, ext_id),
            ).fetchone()
            title = row[0] if row is not None else None
        return "channel", ext_id, (f"беседа «{title}»" if title else f"беседа {ext_id}")
    attr, has = match.get("attr"), match.get("has")
    if attr == "tg_folders":
        folders = tg_folders(conn, schema, account_id) if schema is not None else None
        found = next((f for f in folders or [] if f["id"] == has), None)
        text = f"папка «{found.get('title')}»" if found is not None else f"папка #{has}"
        return "folder", has, text
    if attr == "labels":
        return "label", has, f"метка «{has}»"
    if attr == "from_domains":
        return "domain", has, f"отправитель @{has}"
    return "other", None, _describe_match(match)


def _account_rules_full(conn, account_id: int) -> list[tuple[int, dict, int, int]]:
    rows = conn.execute(
        "SELECT id, match_json, project_id, priority FROM main.map_rule WHERE account_id = ? "
        "ORDER BY priority DESC, id ASC",
        (account_id,),
    ).fetchall()
    return [(rid, json.loads(mj), pid, prio) for rid, mj, pid, prio in rows]


def rule_account(conn, rule_id: int) -> int:
    """`account_id` правила; нет -> `NotFound`."""
    row = conn.execute("SELECT account_id FROM main.map_rule WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise NotFound(f"правило {rule_id} не найдено")
    return row[0]


def rule_description(conn, rule_id: int) -> str:
    row = conn.execute("SELECT account_id, match_json FROM main.map_rule WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise NotFound(f"правило {rule_id} не найдено")
    return describe_rule(conn, row[0], json.loads(row[1]))[2]


def _without_label(match: dict) -> dict:
    return {k: v for k, v in match.items() if k != "label"}


def find_duplicate(conn, account_id: int, match: dict, project_id: int) -> int | None:
    """Решение 3: то же условие (без `label`) того же аккаунта в тот же проект — id существующего правила."""
    bare = _without_label(match)
    for rid, existing, pid, _prio in _account_rules_full(conn, account_id):
        if pid == project_id and _without_label(existing) == bare:
            return rid
    return None


def match_key(match: dict) -> str:
    """Ф38-2 П2: ключ условия — без `label`, в том же виде, в каком правила сравнивает `find_duplicate`
    (переименование папки Telegram ключ не меняет)."""
    return json.dumps(_without_label(match), sort_keys=True, ensure_ascii=False)


def find_other_project(conn, account_id: int, match: dict, project_id: int) -> int | None:
    """Ф38-2 П3: то же условие (без `label`) того же аккаунта уже ведёт в ДРУГОЙ проект — id этого правила.
    Новое правило с тем же условием проиграло бы ему по `id` и не сработало бы никогда."""
    bare = _without_label(match)
    for rid, existing, pid, _prio in _account_rules_full(conn, account_id):
        if pid != project_id and _without_label(existing) == bare:
            return rid
    return None


def check_rule_target(conn, account_id: int, kind: str, value, project_id: int) -> tuple[dict, dict]:
    """Общая проверка цели правила — одна связка на `POST /map-rules` (Ф14-1) и на заявку сессии (Ф38-2):
    условие (`build_match`), проект, дубль. Возвращает `(match, storable)`: первое — для описания, второе —
    то, что ляжет в `main`. Порядок отказов: аккаунт (`NotFound`) / условие (`InvalidMatch`) -> проект
    (`NotFound`) -> дубль (`DuplicateRule`)."""
    match = build_match(conn, account_id, kind, value)
    if (
        project_id <= 0
        or conn.execute("SELECT 1 FROM main.project WHERE id = ?", (project_id,)).fetchone() is None
    ):
        raise NotFound(f"project_id {project_id} не существует")
    storable = storable_match(conn, account_id, match)
    duplicate = find_duplicate(conn, account_id, storable, project_id)
    if duplicate is not None:
        raise DuplicateRule(duplicate)
    return match, storable


def preview(conn, account_id: int, *, add=None, remove: int | None = None) -> dict:
    """Ф14-1 A3: сколько каналов аккаунта сменят проект, если добавить правило `add=(match, project_id,
    priority)` или удалить правило `remove`. Без записи: правила — список в памяти, выбор — тот же
    `_choose_auto`, что у пересчёта, обход — в том же порядке (не-топики, затем топики за своим форумом)."""
    _source, schema = _account_row(conn, account_id)
    rules = _account_rules_full(conn, account_id)
    if remove is not None:
        if not any(r[0] == remove for r in rules):
            raise NotFound(f"правило {remove} не найдено")
        rules = [r for r in rules if r[0] != remove]
    # priority DESC; среди равных настоящее правило раньше кандидата (кандидат получил бы самый большой id).
    keyed = [((-prio, 0, rid), (rid, match, pid)) for rid, match, pid, prio in rules]
    if add is not None:
        match, project_id, priority = add
        if priority is None:
            priority = default_priority(match)
        keyed.append(((-priority, 1, 0), (0, match, project_id)))
    keyed.sort(key=lambda k: k[0])
    ordered = [rule for _key, rule in keyed]

    rows = conn.execute(
        _sql(
            schema,
            "SELECT id, ext_id, kind, parent_channel_id, meta_json, project_id, map_source, title, archived, "
            "last_msg_ts FROM {schema}.channel WHERE account_id = ?",
        ),
        (account_id,),
    ).fetchall()
    new_project: dict[int, int | None] = {}
    topics = []
    for row in rows:
        cid, ext_id, kind, parent_id, meta_json, project_id, map_source = row[:7]
        if kind == "topic" and parent_id is not None:
            topics.append(row)
        elif map_source != "auto":
            new_project[cid] = project_id
        else:
            new_project[cid], _reason = _choose_auto(
                ordered,
                ext_id=ext_id,
                attrs=_channel_attrs(meta_json),
                is_topic=False,
                parent_project_id=None,
            )
    for row in topics:
        cid, _ext_id, _kind, parent_id, _meta_json, project_id, map_source = row[:7]
        if map_source != "auto":
            new_project[cid] = project_id
            continue
        if parent_id in new_project:
            parent_project_id = new_project[parent_id]
        else:
            parent = conn.execute(
                _sql(schema, "SELECT project_id FROM {schema}.channel WHERE id = ?"), (parent_id,)
            ).fetchone()
            parent_project_id = parent[0] if parent is not None else None
        new_project[cid], _reason = _choose_auto(
            ordered, ext_id=_ext_id, attrs=None, is_topic=True, parent_project_id=parent_project_id
        )

    changed = [row for row in rows if new_project[row[0]] != row[5]]
    changed.sort(key=lambda r: (1 if r[8] else 0, -(r[9] or 0), r[0]))
    examples = [
        {"channel_id": r[0], "title": r[7], "from_project_id": r[5], "to_project_id": new_project[r[0]]}
        for r in changed[:_EXAMPLES_LIMIT]
    ]
    return {"moved": len(changed), "examples": examples}


def rules_for_ui(conn) -> list[dict]:
    """Ф14-1 A4 `GET /map-rules`: правила всех аккаунтов в порядке проверки (`account_id`, затем `priority
    DESC, id ASC`) с описанием и числом авто-каналов, разложенных этим правилом сейчас (по `map_reason`)."""
    rows = conn.execute(
        "SELECT r.id, r.account_id, r.match_json, r.project_id, r.priority, a.db, r.origin_human_id "
        "FROM main.map_rule r LEFT JOIN main.account a ON a.id = r.account_id "
        "ORDER BY r.account_id, r.priority DESC, r.id ASC"
    ).fetchall()
    result = []
    for rid, account_id, match_json, project_id, priority, schema, origin_human_id in rows:
        kind, value, text = describe_rule(conn, account_id, json.loads(match_json))
        if origin_human_id is not None:  # Ф95-1 (B.1): заведено заявкой «нужен человек» по домену регистрации
            text = f"{text} (регистрация, заявка #{origin_human_id})"
        channels = 0
        if schema is not None:
            channels = conn.execute(
                _sql(
                    schema,
                    "SELECT COUNT(*) FROM {schema}.channel WHERE account_id = ? AND map_source = 'auto' "
                    "AND map_reason LIKE ?",
                ),
                (account_id, f"правило #{rid}:%"),
            ).fetchone()[0]
        result.append(
            {
                "id": rid,
                "account_id": account_id,
                "project_id": project_id,
                "priority": priority,
                "kind": kind,
                "value": value,
                "description": text,
                "channels": channels,
            }
        )
    return result


def _option_sort_key(item: dict, name_key: str) -> tuple:
    return (-item["unsorted"], -item["channels"], str(item[name_key]))


def map_options(conn, account_id: int) -> dict:
    """Ф14-1 A4 `GET /accounts/{id}/map-options`: из чего можно собрать правило этого аккаунта — виды
    условий, папки Telegram, метки и домены почты со счётом НЕархивных бесед (`unsorted` — из них в
    «Неразобранном»); не больше 100 строк в списке."""
    source, schema = _account_row(conn, account_id)
    rows = conn.execute(
        _sql(
            schema,
            "SELECT meta_json, project_id FROM {schema}.channel WHERE account_id = ? AND archived = 0",
        ),
        (account_id,),
    ).fetchall()

    def _count(attr: str) -> dict:
        counts: dict = {}
        for meta_json, project_id in rows:
            attrs = _channel_attrs(meta_json)
            values = attrs.get(attr) if attrs else None
            if not isinstance(values, list):
                continue
            usable = (v for v in values if isinstance(v, str | int) and not isinstance(v, bool))
            for value in dict.fromkeys(usable):
                total, unsorted = counts.get(value, (0, 0))
                counts[value] = (total + 1, unsorted + (1 if project_id is None else 0))
        return counts

    def _listed(counts: dict, name_key: str) -> list[dict]:
        items = [{name_key: name, "channels": t, "unsorted": u} for name, (t, u) in counts.items()]
        items.sort(key=lambda i: _option_sort_key(i, name_key))
        return items[:_OPTIONS_LIMIT]

    folders: list[dict] = []
    labels: list[dict] = []
    domains: list[dict] = []
    if source == "telegram":
        counts = _count("tg_folders")
        for folder in tg_folders(conn, schema, account_id) or []:
            total, unsorted = counts.get(folder["id"], (0, 0))
            folders.append(
                {"id": folder["id"], "title": folder.get("title"), "channels": total, "unsorted": unsorted}
            )
        folders.sort(key=lambda i: _option_sort_key(i, "title"))
        folders = folders[:_OPTIONS_LIMIT]
        kinds = (["folder"] if folders else []) + ["all"]
    elif source == "mail":
        labels = _listed(_count("labels"), "name")
        domains = _listed(_count("from_domains"), "domain")
        kinds = ["domain"] + (["label"] if labels else []) + ["all"]
    else:
        kinds = ["all"]
    return {
        "account_id": account_id,
        "source": source,
        "kinds": kinds,
        "folders": folders,
        "labels": labels,
        "domains": domains,
    }
