"""Ключевые слова подписки «лично мне» (Ф25-1, docs/architecture.md и поправка П3).

Чистые функции, без SQL. Совпадение — по началу слова: ключ «штраф» ловит «штрафы», но не «оштрафовали»;
фраза — подряд идущие слова («задерж выплат» ловит «задержали выплату»). Регистр и `ё`/`е` не различаются.
Минус-слово — по тому же правилу. Без морфологии и без LLM.

П3: проверка быстрая — `compile(spec)` раскладывает ключи по первым двум буквам первого слова (слово ключа не
короче `WORD_MIN = 2`, поэтому слово текста `w` может совпасть с ключом, только если `w[:2]` равно этим двум
буквам), внутри — по первому слову целиком; `compile` — один раз на запрос или сигнал, `check(compiled, text)`
— на каждое сообщение.

Ф36-1 E: слово короче `WORD_MIN` допустимо внутри фразы («а/б-тест», «к оплате») и совпадает только целиком;
первое короткое слово ключа лежит в корзине своего единственного символа — слово текста попадает в неё, только
если само равно этому символу. Ф36-1 A: в той же паре хранится `batch_min` — раз во сколько минут подписке
отдаются сигналы «по ключу» (`batch_ms`).

Ф67-1: интервал пачки — у любой подписки, и без ключей. Подписка без ключей с интервалом хранит пару с пустыми
списками `{"keys": [], "exclude": [], "batch_min": N}` (`N > 0`; без интервала — по-прежнему `NULL`).
`parse_spec` такую пару не видит (ключей нет — `None`): «есть ли ключи» — `has_keys`, интервал —
`stored_batch` и `batch_ms`. Умолчание интервала: с ключами — `BATCH_MIN_DEFAULT`, без ключей — 0 (сразу).
"""

import json
import re
from typing import NamedTuple

KEYS_MAX = 100
EXCLUDE_MAX = 100
WORDS_MAX = 3
RULE_WORDS_MAX = 6  # Ф63-1 П6: фраза правила входящих проекта («новое сообщение по заказу»)
WORD_MIN = 2
WORD_MAX = 30
TEXT_WORDS_MAX = 1000
SIGNAL_KEYS_MAX = 5
# Ф36-1 A1/A2: сигналы «по ключу» отдаются пачкой раз в `batch_min` минут; ключа в `keywords_json` нет —
# умолчание, 0 — сразу.
BATCH_MIN_DEFAULT = 15
BATCH_MIN_MAX = 120

# Форма нормализованного ключа (её же проверяют `sub_wait` и формат MCP перед выводом). Только `fullmatch`:
# `$` пропустил бы хвостовой перевод строки.
# Ф36-1 E3: слово в один символ форма пропускает; «только внутри фразы» проверяет `normalize`.
KEY_RE = re.compile(r"[^\W_]{1,30}(?: [^\W_]{1,30}){0,2}")
_WORD_RE = re.compile(r"[^\W_]+")


class Invalid(ValueError):
    """Список ключей не годится: текст называет номер элемента (с 1) и причину."""


def words(text: str) -> list[str]:
    """Слова текста: `casefold`, `ё` -> `е`, буквы любых алфавитов и цифры; не больше `TEXT_WORDS_MAX`
    первых."""
    folded = text.casefold().replace("ё", "е")
    out: list[str] = []
    for match in _WORD_RE.finditer(folded):
        out.append(match.group())
        if len(out) >= TEXT_WORDS_MAX:
            break
    return out


def normalize(values, limit: int, *, what: str = "ключ", words_max: int = WORDS_MAX) -> list[str]:
    """Список строк -> нормализованные ключи (слова через один пробел, дубли убраны, порядок первого
    появления). Отказ — `Invalid` с номером элемента и причиной. Ф63-1 П6: `words_max` — слов во фразе (у
    подписок `WORDS_MAX = 3`, у правил входящих проекта — `RULE_WORDS_MAX = 6`)."""
    if not isinstance(values, list):
        raise Invalid(f"{what}: нужен список строк")
    out: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values, start=1):
        if not isinstance(value, str):
            raise Invalid(f"{what} {index}: не строка")
        parts = words(value)
        if not parts:
            raise Invalid(f"{what} {index}: нет ни одного слова")
        if len(parts) > words_max:
            raise Invalid(f"{what} {index}: больше {words_max} слов")
        # Ф36-1 E1: короткое слово — только во фразе, где есть хотя бы одно слово не короче `WORD_MIN`.
        phrase = len(parts) >= 2 and any(len(part) >= WORD_MIN for part in parts)
        for part in parts:
            if len(part) < WORD_MIN and not phrase:
                raise Invalid(f"{what} {index}: слово короче {WORD_MIN} букв — можно только внутри фразы")
            if len(part) > WORD_MAX:
                raise Invalid(f"{what} {index}: слово длиннее {WORD_MAX} букв")
        key = " ".join(parts)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) > limit:
            raise Invalid(f"{what} {index}: больше {limit} в списке")
    return out


def _word_hit(text_word: str, key_word: str) -> bool:
    """Ф36-1 E2: слово ключа короче `WORD_MIN` совпадает только целиком, длинное — по началу."""
    return text_word == key_word if len(key_word) < WORD_MIN else text_word.startswith(key_word)


def matches(keys: list[str], text_words: list[str]) -> list[str]:
    """Ключи, у которых есть позиция `i`, где `text_words[i + j]` совпадает с `key_words[j]` (`_word_hit`) для
    всех `j`. Порядок — как в списке ключей. Простая проверка (без раскладки) — для тестов и малых списков."""
    found: list[str] = []
    for key in keys:
        key_words = key.split(" ")
        n = len(key_words)
        for i in range(len(text_words) - n + 1):
            if all(_word_hit(text_words[i + j], key_words[j]) for j in range(n)):
                found.append(key)
                break
    return found


_Cand = tuple[int, str, tuple[str, ...]]  # (номер ключа в списке, ключ, его слова)


class _Bucket(NamedTuple):
    """Ключи с одними первыми двумя буквами: по первому слову целиком и длины первых слов по возрастанию —
    слово текста `w` проверяется `w[:L]` для каждой длины, а не перебором всех ключей корзины (сто ключей
    на «ш» не превращают письмо в 100 000 сравнений)."""

    by_first: dict[str, list[_Cand]]
    lengths: tuple[int, ...]


class _Index(NamedTuple):
    by_prefix: dict[str, _Bucket]  # первые 2 буквы первого слова -> корзина


class Compiled(NamedTuple):
    keys: _Index
    exclude: _Index


def _index(keys: list[str]) -> _Index:
    raw: dict[str, dict[str, list[_Cand]]] = {}
    for order, key in enumerate(keys):
        key_words = tuple(key.split(" "))
        first = key_words[0]
        raw.setdefault(first[:WORD_MIN], {}).setdefault(first, []).append((order, key, key_words))
    return _Index(
        {
            prefix: _Bucket(by_first, tuple(sorted({len(first) for first in by_first})))
            for prefix, by_first in raw.items()
        }
    )


def _scan(index: _Index, text_words: list[str], *, first_only: bool) -> list[tuple[int, str]]:
    found: dict[int, str] = {}
    count = len(text_words)
    for i, word in enumerate(text_words):
        bucket = index.by_prefix.get(word[:WORD_MIN])
        if bucket is None:
            continue
        for length in bucket.lengths:
            if length > len(word):
                break
            for order, key, key_words in bucket.by_first.get(word[:length], ()):
                if order in found:
                    continue
                n = len(key_words)
                if i + n > count:
                    continue
                if all(_word_hit(text_words[i + j], key_words[j]) for j in range(1, n)):
                    found[order] = key
                    if first_only:
                        return [(order, key)]
    return sorted(found.items())


def compile(spec: dict | None) -> Compiled | None:  # noqa: A001 — имя из спеки (П3)
    """Нормализованная пара `{"keys": [...], "exclude": [...]}` (как в `session_sub.keywords_json`) ->
    раскладка для `check`. Нет ключей — `None`."""
    if not isinstance(spec, dict):
        return None
    keys = [k for k in spec.get("keys") or [] if isinstance(k, str) and k]
    if not keys:
        return None
    exclude = [k for k in spec.get("exclude") or [] if isinstance(k, str) and k]
    return Compiled(_index(keys), _index(exclude))


def check(compiled: Compiled | None, text: str | None) -> list[str] | None:
    """Совпавшие ключи (порядок — как в списке ключей); `None` — ключей нет, ни один не совпал или есть
    минус-слово."""
    if compiled is None or not text:
        return None
    text_words = words(text)
    found = _scan(compiled.keys, text_words, first_only=False)
    if not found:
        return None
    if compiled.exclude.by_prefix and _scan(compiled.exclude, text_words, first_only=True):
        return None
    return [key for _order, key in found]


def parse_spec(raw: str | None) -> dict | None:
    """`session_sub.keywords_json` -> `{"keys": [...], "exclude": [...]}` или `None` (нет или испорчено).
    Ф36-1 A2: плюс `batch_min`, если в JSON целое 0..`BATCH_MIN_MAX` (иначе ключа в результате нет)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("keys"), list) or not value["keys"]:
        return None
    exclude = value.get("exclude")
    spec: dict = {"keys": list(value["keys"]), "exclude": list(exclude) if isinstance(exclude, list) else []}
    batch_min = value.get("batch_min")
    if isinstance(batch_min, int) and not isinstance(batch_min, bool) and 0 <= batch_min <= BATCH_MIN_MAX:
        spec["batch_min"] = batch_min
    return spec


def _load(raw: str | None) -> dict | None:
    """Ф67-1: сырой `keywords_json` -> словарь JSON (и пара без ключей) или `None`."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError, TypeError:
        return None
    return value if isinstance(value, dict) else None


def _valid_batch(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= BATCH_MIN_MAX


def has_keys(raw: str | None) -> bool:
    """Ф67-1: есть ли у `keywords_json` ключи (пара только с интервалом — нет)."""
    return parse_spec(raw) is not None


def batch_ms(raw_or_spec: str | dict | None) -> int:
    """Ф36-1 A2 + Ф67-1: действующий интервал пачки подписки в миллисекундах — по `keywords_json` или уже
    разобранной паре. Записанный `batch_min` — он; не записан — с ключами `BATCH_MIN_DEFAULT`, без ключей 0
    (сразу)."""
    spec = _load(raw_or_spec) if isinstance(raw_or_spec, str) else raw_or_spec
    if not isinstance(spec, dict):
        return 0
    batch_min = spec.get("batch_min")
    if _valid_batch(batch_min):
        return batch_min * 60_000
    keys = spec.get("keys")
    return BATCH_MIN_DEFAULT * 60_000 if isinstance(keys, list) and keys else 0


def batch_minutes(raw: str | None) -> int:
    """Ф67-1: действующий интервал пачки в минутах (0 — сразу) — для строк подписок."""
    return batch_ms(raw) // 60_000


def stored_batch(raw: str | None) -> int | None:
    """Ф36-1 A3 + Ф67-1: `batch_min`, записанный в `keywords_json` (с ключами или без; `None` — нет)."""
    value = _load(raw)
    batch_min = value.get("batch_min") if value is not None else None
    return batch_min if _valid_batch(batch_min) else None


def with_batch(raw: str | None, batch_min: int | None) -> str | None:
    """Ф36-1 A3 + Ф67-1: тот же `keywords_json` с интервалом `batch_min`; `None` — интервал не пишется (и
    прежний убирается). Ключей нет — пара только с интервалом, если он больше 0, иначе `None` (сразу — как
    без пары)."""
    spec = parse_spec(raw)
    if spec is None:
        if batch_min is None or batch_min <= 0:
            return None
        return json.dumps({"keys": [], "exclude": [], "batch_min": batch_min}, ensure_ascii=False)
    return dump_spec(spec["keys"], spec["exclude"], batch_min)


def batch_only(raw: str | None) -> str | None:
    """Ф67-1: из `keywords_json` — только записанный интервал (ключи сняты): смена фильтра на «все» или
    «карточки», наследование подпиской без ключей."""
    return with_batch(None, stored_batch(raw))


def public_spec(raw: str | None) -> dict | None:
    """Ф36-1 A4: пара для строки подписки (`sub_json`/`chan_json`): `batch_min` — ДЕЙСТВУЮЩЕЕ значение (с
    умолчанием). Ключей нет — `None` (интервал без ключей — отдельным полем строки, `batch_minutes`)."""
    spec = parse_spec(raw)
    if spec is None:
        return None
    spec["batch_min"] = batch_ms(spec) // 60_000
    return spec


def safe_keys(value) -> list[str] | None:
    """Ключи из чужих рук (вывод ожидания, формат MCP): список строк, каждая — `KEY_RE.fullmatch`, не больше
    `SIGNAL_KEYS_MAX`; иначе `None` (поле отбрасывается целиком)."""
    if not isinstance(value, list) or not value or len(value) > SIGNAL_KEYS_MAX:
        return None
    if not all(isinstance(k, str) and KEY_RE.fullmatch(k) for k in value):
        return None
    return list(value)


def dump_spec(keys: list[str], exclude: list[str], batch_min: int | None = None) -> str | None:
    """Нормализованные списки -> `session_sub.keywords_json`; без ключей — `None`. Ф36-1 A2: `batch_min`
    пишется, только когда не `None`."""
    if not keys:
        return None
    value: dict = {"keys": keys, "exclude": exclude}
    if batch_min is not None:
        value["batch_min"] = batch_min
    return json.dumps(value, ensure_ascii=False)


def spec_counts(raw: str | None) -> tuple[int, int]:
    """Числа для аудита: `(ключей, минус-слов)`."""
    spec = parse_spec(raw)
    if spec is None:
        return 0, 0
    return len(spec["keys"]), len(spec["exclude"])
