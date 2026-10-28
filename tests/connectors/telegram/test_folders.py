"""Тест 7 куска 1b: `folders.py` — вычисление принадлежности диалогов папкам. Чистая функция, без сети."""

from telethon.tl import types

from hub.connectors.telegram import folders as f

from . import helpers as h


def _flags(marked_id, dtype, *, contact=False, muted=False, archived=False) -> f.DialogFlags:
    return f.DialogFlags(marked_id=marked_id, dtype=dtype, contact=contact, muted=muted, archived=archived)


def test_1b_07_flags_contacts_non_contacts_groups_broadcasts_bots():
    contact_user = _flags(1, "user", contact=True)
    stranger = _flags(2, "user", contact=False)
    group = _flags(-3, "group")
    broadcast = _flags(4, "broadcast")
    bot = _flags(5, "bot")
    dialogs = [contact_user, stranger, group, broadcast, bot]

    filt_contacts = h.make_dialog_filter(1, contacts=True)
    filt_non_contacts = h.make_dialog_filter(2, non_contacts=True)
    filt_groups = h.make_dialog_filter(3, groups=True)
    filt_broadcasts = h.make_dialog_filter(4, broadcasts=True)
    filt_bots = h.make_dialog_filter(5, bots=True)

    result = f.compute_folders(
        dialogs, [filt_contacts, filt_non_contacts, filt_groups, filt_broadcasts, filt_bots], me_marked_id=999
    )

    assert result[1] == [1]
    assert result[2] == [2]
    assert result[-3] == [3]
    assert result[4] == [4]
    assert result[5] == [5]


def test_1b_07_include_exclude_pinned():
    d1 = _flags(1, "user")
    d2 = _flags(2, "user")
    d3 = _flags(3, "user")
    dialogs = [d1, d2, d3]

    filt = h.make_dialog_filter(
        1,
        include=[types.InputPeerUser(1, 0), types.InputPeerUser(2, 0)],
        exclude=[types.InputPeerUser(2, 0)],
        pinned=[types.InputPeerUser(3, 0)],
    )
    result = f.compute_folders(dialogs, [filt], me_marked_id=999)
    assert result[1] == [1]  # include, не в exclude
    assert result[2] == []  # include, НО в exclude -> исключён
    assert result[3] == [1]  # pinned


def test_1b_07_exclude_muted_and_exclude_archived():
    muted = _flags(1, "group", muted=True)
    archived = _flags(2, "group", archived=True)
    plain = _flags(3, "group")
    dialogs = [muted, archived, plain]

    filt = h.make_dialog_filter(1, groups=True, exclude_muted=True, exclude_archived=True)
    result = f.compute_folders(dialogs, [filt], me_marked_id=999)
    assert result[1] == []
    assert result[2] == []
    assert result[3] == [1]


def test_1b_07_exclude_read_is_ignored():
    """`exclude_read` — решение 11.5: чат не должен прыгать между проектами при каждом прочтении. Флаг
    хранится в `DialogFilter`, но `compute_folders` его не смотрит вовсе."""
    d = _flags(1, "group")
    filt = h.make_dialog_filter(1, groups=True, exclude_read=True)
    result = f.compute_folders([d], [filt], me_marked_id=999)
    assert result[1] == [1]


def test_1b_07_chatlist_only_include_and_pinned_no_flags():
    d1 = _flags(1, "group")  # был бы groups=True, но Chatlist флаги не смотрит вовсе
    d2 = _flags(2, "user")
    dialogs = [d1, d2]
    chatlist = types.DialogFilterChatlist(
        id=9,
        title=types.TextWithEntities(text="Список", entities=[]),
        pinned_peers=[types.InputPeerUser(2, 0)],
        include_peers=[],
    )
    result = f.compute_folders(dialogs, [chatlist], me_marked_id=999)
    assert result[1] == []
    assert result[2] == [9]


def test_1b_07_default_filter_is_skipped():
    d = _flags(1, "group")
    default = types.DialogFilterDefault()
    result = f.compute_folders([d], [default], me_marked_id=999)
    assert result[1] == []


def test_1b_07_matching_is_by_id_only_not_by_name():
    """Диалог с тем же ИМЕНЕМ, но другим marked id, не должен матчиться include_peers."""
    d = _flags(42, "user")
    filt = h.make_dialog_filter(1, include=[types.InputPeerUser(999, 0)])
    result = f.compute_folders([d], [filt], me_marked_id=1)
    assert result[42] == []


def test_1b_07_channel_attrs_shape():
    d = _flags(1, "group", contact=False, muted=True, archived=False)
    attrs = f.channel_attrs(d, tg_type="megagroup", folders=[3, 1])
    assert attrs == {
        "tg_type": "megagroup",
        "contact": False,
        "muted": True,
        "archived": False,
        "tg_folders": [3, 1],
    }


def test_1b_07_account_meta_folders_list_skips_default():
    filt1 = h.make_dialog_filter(1, title="Работа")
    default = types.DialogFilterDefault()
    out = f.folders_account_meta([filt1, default])
    assert out == [{"id": 1, "title": "Работа"}]
