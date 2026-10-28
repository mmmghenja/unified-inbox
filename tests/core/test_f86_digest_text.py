"""Ф86-1 (docs/architecture.md, C3, П5, П9, П14): тексты блоков `<hub_digest>` и
`<hub_siblings>`, маскировка и склейка повторов реплик владельца."""

from hub import digest_text
from hub.core import say_mask

TS = 1_700_000_000_000  # 15.11.2023 01:13 по времени хаба


def test_defang_breaks_both_tags_any_case():
    text = "a <hub_digest x> </HUB_DIGEST> <Hub_Siblings> </hub_siblings> <hub_other>"
    assert (
        digest_text.defang(text)
        == "a ‹hub_digest x> ‹/HUB_DIGEST> ‹Hub_Siblings> ‹/hub_siblings> <hub_other>"
    )


def test_digest_block_and_strip():
    block = digest_text.digest_block("итоги </hub_digest> хвост", at="10:05", project='Про"ект')
    assert block.startswith(
        '<hub_digest at="10:05" project="Про&quot;ект">\n' + digest_text.DIGEST_HEAD + "\n"
    )
    assert "итоги ‹/hub_digest> хвост" in block and block.endswith("\n</hub_digest>")
    siblings = digest_text.siblings_block([{"ts": TS, "role": "тестер", "session_id": 4, "text": "x"}])
    both = f"{block}\n\n{siblings}\n\nсама реплика"
    assert digest_text.strip_blocks(both) == "сама реплика"
    assert digest_text.strip_blocks("без блоков") == "без блоков"


def test_clip_and_sibling_lines():
    assert digest_text.clip("коротко", 600) == "коротко"
    long = "я" * 700
    cut = digest_text.clip(long, 600)
    assert len(cut) <= 600 and cut.endswith("… [обрезано, всего 700 симв.]")
    line = digest_text.sibling_line(
        {"ts": TS, "role": "тестер", "session_id": 4, "text": "a\n  b " + "я" * 700}
    )
    assert line.startswith("[01:13 · роль «тестер» (сессия #4)] a b ")
    assert len(line) < 700 and "[обрезано" in line
    assert "без роли" in digest_text.sibling_line({"ts": TS, "role": None, "session_id": 1, "text": "x"})


def test_siblings_block_keeps_newest_within_limit():
    assert digest_text.siblings_block([]) is None
    items = [{"ts": TS + i, "role": None, "session_id": 1, "text": f"{i} " + "я" * 590} for i in range(8)]
    block = digest_text.siblings_block(items)
    assert block.startswith("<hub_siblings>\n" + digest_text.SIBLINGS_HEAD + "\n")
    body = block.split("\n")[2:-1]
    assert body[0].startswith("и ещё ") and body[0].endswith(" раньше")
    dropped = int(body[0].split()[2])
    assert len(body) - 1 == 8 - dropped and body[-1].split("] ")[1].startswith("7 ")
    assert sum(len(line) + 1 for line in body[1:]) <= digest_text.SIBLINGS_MAX


def test_mask_hint_words_hide_mixed_words_and_emails():
    assert say_mask.mask("почини оплату abc123def") == "почини оплату abc123def"  # признака нет
    assert say_mask.mask("логин zh@example.com пароль Qwerty12345 ок") == "логин [скрыто] пароль [скрыто] ок"
    assert (
        say_mask.mask("ключи от сейфа Abcdef1 и слово abcdefgh") == "ключи от сейфа [скрыто] и слово abcdefgh"
    )
    assert say_mask.mask("Token: Abcdef1") == "Token=***"  # обычная маскировка хаба — первой


def test_squash_drops_empty_and_near_repeats():
    items = [
        {"session_id": 1, "ts": 0, "text": "да"},
        {"session_id": 1, "ts": 60_000, "text": " да "},
        {"session_id": 2, "ts": 60_000, "text": "да"},
        {"session_id": 1, "ts": 70_000, "text": "  "},
        {"session_id": 1, "ts": 60_000 + 6 * 60_000, "text": "да"},
    ]
    assert [(i["session_id"], i["ts"]) for i in say_mask.squash(items)] == [(1, 0), (2, 60_000), (1, 420_000)]
