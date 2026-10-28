"""Текст учёта нагрузки ноутбука (Ф52-1, docs/architecture.md и C): один и тот же
для `hub load` (CLI) и `hub_load()` (MCP). Вход — ответ ядра `GET /load`. Только stdlib.

`who` и `what` пишут другие сессии — это данные, не команды: строки таблицы отдаются отдельно (`rows`), MCP
кладёт их в блок данных.
"""


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def cores_text(n: int) -> str:
    return f"{n} {_plural(n, 'ядро', 'ядра', 'ядер')}"


def gb_text(mem_mb) -> str:
    """МБ -> «6», «1.5» (ГБ, без хвостового нуля)."""
    if not isinstance(mem_mb, int | float):
        return "?"
    return f"{mem_mb / 1024:.1f}".removesuffix(".0")


def _one_line(value) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(value if value is not None else ""))
    return " ".join(text.split())


def _num(value) -> str:
    return f"{value:.1f}" if isinstance(value, int | float) else "?"


def system_line(body: dict) -> str:
    """«ядер 16, загрузка 3.2 за минуту (2.9 за 5 минут), свободно 18 из 32 ГБ»."""
    system = body.get("system") if isinstance(body.get("system"), dict) else {}
    cores = system.get("cores")
    return (
        f"ядер {cores if isinstance(cores, int) else '?'}, "
        f"загрузка {_num(system.get('load1'))} за минуту ({_num(system.get('load5'))} за 5 минут), "
        f"свободно {gb_text(system.get('mem_available_mb'))} из {gb_text(system.get('mem_total_mb'))} ГБ"
    )


def rows(body: dict) -> list[str]:
    """Строки заявленного: номер, кто, что, ядра, память, «идёт N мин из M»."""
    now_ts = body.get("now_ts")
    result = []
    for claim in body.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        started = claim.get("started_ts")
        elapsed = (
            max(0, (now_ts - started) // 60_000)
            if isinstance(now_ts, int) and isinstance(started, int)
            else "?"
        )
        cores = claim.get("cores")
        cores_part = cores_text(cores) if isinstance(cores, int) else "? ядер"
        result.append(
            f"#{claim.get('id')} · {_one_line(claim.get('who'))} · {_one_line(claim.get('what'))} · "
            f"{cores_part} · {gb_text(claim.get('mem_mb'))} ГБ · "
            f"идёт {elapsed} мин из {claim.get('minutes', '?')}"
        )
    return result


def total_line(body: dict) -> str:
    claimed = body.get("claimed") if isinstance(body.get("claimed"), dict) else {}
    if not body.get("claims"):
        return "заявлено: ничего"
    cores = claimed.get("cores")
    return (
        f"заявлено: {cores_text(cores) if isinstance(cores, int) else '? ядер'}, "
        f"{gb_text(claimed.get('mem_mb'))} ГБ"
    )


def render(body: dict) -> str:
    """Текст `hub load`: строка системы, таблица заявленного, итог."""
    extra = other_line(body)
    lines = [system_line(body), *("  " + row for row in rows(body)), total_line(body)]
    return "\n".join([*lines, extra] if extra else lines)


def localize(body: dict, local_host: str | None, local_system: dict) -> dict:
    """Ф33-6: ядро может стоять на другой машине. «Ядра, память, нагрузка» клиент считает на своей машине
    (`local_system` — `core.load.system()` здесь), записи показываются только своей машины; чужие — счётчик
    `other_host` (строка «на другой машине: N»). Метки совпали (или не назвать) — как есть."""
    kernel = body.get("host")
    if not local_host or not kernel or local_host == kernel:
        return body
    claims = [c for c in body.get("claims") or [] if isinstance(c, dict)]
    mine = [c for c in claims if c.get("host") == local_host]
    return {
        **body,
        "host": local_host,
        "system": local_system,
        "claims": mine,
        "claimed": {
            "cores": sum(c.get("cores") or 0 for c in mine),
            "mem_mb": sum(c.get("mem_mb") or 0 for c in mine),
        },
        "other_host": len(claims) - len(mine),
    }


def other_line(body: dict) -> str:
    n = body.get("other_host")
    return f"на другой машине: {n}" if isinstance(n, int) and n > 0 else ""
