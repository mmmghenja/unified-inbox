"""Вычисление принадлежности диалогов папкам Telegram (`DialogFilter`) — ЧИСТАЯ функция, план раздел 3 и
`docs/architecture.md`.5: «Принадлежность чата папке вычисляет коннектор». Спецификация:
`docs/architecture.md`.

Правило (план, раздел 3 — источник истины; исправлено находкой F15, round 2 критика 1b): диалог в папке,
если он ЯВНО в `include_peers ∪ pinned_peers` (тогда `exclude_muted`/`exclude_archived` его НЕ выкидывают —
это фильтры только для чатов, попавших в папку ПО ФЛАГУ; `exclude_peers` выкидывает ВСЕГДА, и явно
добавленный чат тоже) ИЛИ подходит под один из флагов (`contacts`/`non_contacts`/`groups`/`broadcasts`/
`bots`) и при этом НЕ (`exclude_muted` и `muted`), НЕ (`exclude_archived` и `archived`). `exclude_read`
ИГНОРИРУЕТСЯ (иначе чат прыгал бы между проектами при каждом прочтении — решение 11.5). Сопоставление СТРОГО
по помеченному id (`utils.get_peer_id`), никогда по имени. `DialogFilterChatlist` — только `include_peers ∪
pinned_peers`, флаги и excludes не применяются вовсе. `DialogFilterDefault` — без id/title, пропускается
целиком.
"""

from __future__ import annotations

from dataclasses import dataclass

from telethon import utils
from telethon.tl import types


@dataclass(frozen=True)
class DialogFlags:
    """Вход на один диалог — то немногое, что нужно правилам папок (план, раздел 3)."""

    marked_id: int
    dtype: str  # "user" | "bot" | "group" | "broadcast" — тип для флагов фильтра (contacts/groups/bots/…)
    contact: bool  # для пользователя (себя считать контактом — раздел «Папки -> attrs»); иначе неважно
    muted: bool
    archived: bool


def dialog_type(entity: object) -> str:
    """`user|bot|group|broadcast` — для сопоставления с флагами `DialogFilter` (НЕ то же самое, что
    `convert.dialog_kind`: там форум и мегагруппа обе "group", здесь тоже "group"; различие только
    user/bot — `DialogFilter.contacts/non_contacts` смотрят только на пользователей-не-ботов)."""
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, types.Chat | types.ChatForbidden):
        return "group"
    if isinstance(entity, types.Channel):
        return "group" if entity.megagroup else "broadcast"
    return "user"


def _peer_marked_id(peer: object, *, me_marked_id: int) -> int | None:
    if isinstance(peer, types.InputPeerSelf):
        return me_marked_id
    try:
        return utils.get_peer_id(peer)
    except TypeError:
        return None


def _member_ids(peers: list, *, me_marked_id: int) -> set[int]:
    out = set()
    for p in peers:
        mid = _peer_marked_id(p, me_marked_id=me_marked_id)
        if mid is not None:
            out.add(mid)
    return out


def compute_folders(
    dialogs: list[DialogFlags], dialog_filters: list, *, me_marked_id: int
) -> dict[int, list[int]]:
    """`marked_id -> [id папки, ...]` по возрастанию, для КАЖДОГО диалога из `dialogs` (пустой список — не
    ни в одной папке). `dialog_filters` — сырые `messages.GetDialogFiltersRequest().filters`."""
    result: dict[int, list[int]] = {d.marked_id: [] for d in dialogs}

    for f in dialog_filters:
        if isinstance(f, types.DialogFilterDefault):
            continue  # без id/title — не папка, пропускается целиком

        pinned_ids = _member_ids(f.pinned_peers, me_marked_id=me_marked_id)
        include_ids = _member_ids(f.include_peers, me_marked_id=me_marked_id)

        if isinstance(f, types.DialogFilterChatlist):
            for mid in pinned_ids | include_ids:
                if mid in result:
                    result[mid].append(f.id)
            continue

        if not isinstance(f, types.DialogFilter):
            continue  # неизвестный будущий подтип — не роняем сверку, просто игнорируем

        exclude_ids = _member_ids(f.exclude_peers, me_marked_id=me_marked_id)
        for d in dialogs:
            explicit = d.marked_id in include_ids or d.marked_id in pinned_ids
            by_flag = (
                (f.contacts and d.dtype == "user" and d.contact)
                or (f.non_contacts and d.dtype == "user" and not d.contact)
                or (f.groups and d.dtype == "group")
                or (f.broadcasts and d.dtype == "broadcast")
                or (f.bots and d.dtype == "bot")
            )
            if not (explicit or by_flag):
                continue
            if d.marked_id in exclude_ids:
                continue
            # Находка F15 (round 2): exclude_muted/exclude_archived — фильтры ТОЛЬКО для чатов, попавших в
            # папку ПО ФЛАГУ. Явно добавленный (`include_peers`/`pinned_peers`) чат ими не выкидывается —
            # раньше «Работа» (замьюченная явно добавленная группа) с `exclude_muted=True` пропадала бы из
            # папки, хотя владелец положил её туда СВОИМИ РУКАМИ.
            if not explicit:
                if f.exclude_muted and d.muted:
                    continue
                if f.exclude_archived and d.archived:
                    continue
            # exclude_read сознательно НЕ проверяется (решение 11.5)
            result[d.marked_id].append(f.id)

    for ids in result.values():
        ids.sort()
    return result


def channel_attrs(dialog: DialogFlags, *, tg_type: str, folders: list[int]) -> dict:
    """`attrs` канала (раздел «Папки -> attrs»): `tg_type` — подробный тип у источника (заполняет вызывающий:
    user|bot|group|megagroup|broadcast|forum), остальное — из `DialogFlags`/вычисленных папок."""
    return {
        "tg_type": tg_type,
        "contact": dialog.contact,
        "muted": dialog.muted,
        "archived": dialog.archived,
        "tg_folders": list(folders),
    }


def folders_account_meta(dialog_filters: list) -> list[dict]:
    """`account_meta` со списком папок (`{"id","title"}`, `title.text` — `TextWithEntities`) для `hub folders`
    (раздел «Папки -> attrs»). `DialogFilterDefault` пропускается — у него нет `id`/`title`."""
    out = []
    for f in dialog_filters:
        if isinstance(f, types.DialogFilterDefault):
            continue
        title = getattr(f, "title", None)
        text = title.text if title is not None else ""
        out.append({"id": f.id, "title": text})
    return out
