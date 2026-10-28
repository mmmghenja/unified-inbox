"""Публичный чекер `scripts/check-private.sh` (`scripts/check_private.py`): срабатывает на личное и секреты,
не срабатывает на разрешённое, значение не печатает. Образцы собираются из кусков во время теста — в файле
теста их нет целиком, иначе чекер нашёл бы их в самом репозитории."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check-private.sh"
_spec = importlib.util.spec_from_file_location("check_private", REPO / "scripts" / "check_private.py")
assert _spec is not None and _spec.loader is not None
cp = importlib.util.module_from_spec(_spec)
sys.modules["check_private"] = cp
_spec.loader.exec_module(cp)


def j(*parts: str) -> str:
    return "".join(parts)


POSITIVE = [
    ("email", j("ivan.petrov", "@", "gmail", ".com")),
    ("email", j("Contact: a", "@", "corp", ".io;")),
    ("phone", j("+7 9", "16 123-45-67")),
    ("phone", j("8 (9", "26) 555 12 34")),
    ("phone", j("7", "9161234567")),
    ("phone", j("+4", "4", "2071234567")),
    ("phone", j("+7 900 1", "23-45-67")),  # вне тестового диапазона +7 900 0XX
    ("private-ip", j("ssh 192", ".168.0.10")),
    ("private-ip", j("host=10", ".1.2.3")),
    ("private-ip", j("172", ".20.0.5")),
    ("slack-token", j("xo", "xp-1234-5678-abcdefghij")),
    ("slack-token", j("xa", "pp-1-A0B1C2D3E4-5678")),
    ("api-key", j("s", "k-ant-", "a" * 30)),
    ("api-key", j("AK", "IA", "ABCDEFGHIJKLMNOP")),
    ("api-key", j("gh", "p_", "A" * 36)),
    ("bot-token", j("123456789", ":AA", "b" * 33)),
    ("private-key", j("-----BEGIN ", "OPENSSH PRIVATE", " KEY-----")),
    ("private-key", j("AGE-SECRET", "-KEY-1", "QQQQQQQQQQQQQQQQQQQQQQ")),
    ("api-hash", j("api_", "hash = '", "0123456789abcdef" * 2, "'")),
    ("inn", j("ИНН ", "77070", "83893")),
    ("inn", j("inn: ", "500100", "732259")),
    ("ogrn", j("ОГРН ", "1027700", "132195")),
    ("passport", j("паспорт ", "45 06 12", "3456")),
    ("card", j("4111 ", "1111 1111 1111")),
    ("card", j("номер карты ", "41111111", "11111111")),
]

NEGATIVE = [
    j("owner", "@", "example.com"),
    j("bot", "@", "mail.example.com"),
    j("a", "@", "acme.test"),
    j("x", "@", "shop.example"),
    j("y", "@", "nowhere.invalid"),
    j("+7 900 ", "000-00-01"),
    j("7900", "0001234"),
    j("+7 900 0", "14-56-67"),
    "127.0.0.1 и 8.8.8.8",
    j("192", ".0.2.15"),  # документационная сеть, не частная
    j("xo", "xp-short"),
    j("ИНН ", "7707083894"),  # контрольная цифра не сходится
    j("ОГРН ", "1027700132196"),
    j("4111 ", "1111 1111 1112"),  # Луна не сходится
    "1700000000000 1099511627837",  # время и id
    j("vue", "@", "3.5.13"),
    j("hub-connector", "@", "11.service и hub-sync@2.timer"),
]


@pytest.mark.parametrize(("rule", "text"), POSITIVE)
def test_rule_fires(rule, text):
    assert rule in cp.line_findings(text, set())


@pytest.mark.parametrize("text", NEGATIVE)
def test_allowed_does_not_fire(text):
    assert cp.line_findings(text, set()) == []


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(SCRIPT), str(root)], capture_output=True, text=True, timeout=60, check=False
    )


def test_cli_reports_place_and_rule_but_never_the_value(tmp_path):
    secret = j("ivan.petrov", "@", "gmail", ".com")
    _write(tmp_path, "src/a.py", f"x = 1\nADDR = '{secret}'\n")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert "src/a.py:2: email" in r.stdout
    assert secret not in r.stdout + r.stderr and "ivan" not in r.stdout + r.stderr


def test_marker_on_line_or_comment_above_and_allow_file(tmp_path):
    ip = j("192", ".168.1.5")
    _write(tmp_path, "t/a.py", f"A = '{ip}'  # showcase-ok: частная сеть — суть теста\n")
    _write(tmp_path, "t/b.ts", f"// showcase-ok: частная сеть — суть теста\nconst b = '{ip}'\n")
    _write(tmp_path, "t/c.py", f"# комментарий без метки\nC = '{ip}'\n")
    _write(tmp_path, "t/d.py", f"D = '{ip}'\n")
    _write(tmp_path, ".check-private-allow", "t/d.py private-ip  # разобрано\nexample.co email-domain\n")
    _write(tmp_path, "t/e.py", j("E = 'a", "@", "example.co'\n"))
    r = _run(tmp_path)
    assert r.returncode == 1
    assert r.stdout.splitlines()[:-1] == ["t/c.py:2: private-ip"]


def test_email_domain_allowed_only_under_its_glob(tmp_path):
    addr = j("a", "@", "corp", ".io")
    _write(tmp_path, "tests/t.py", f"A = '{addr}'\n")
    _write(tmp_path, "src/s.py", f"A = '{addr}'\n")
    _write(tmp_path, "docs/d.md", j("b", "@", "x", ".ru\n"))
    _write(tmp_path, ".check-private-allow", "corp.io email-domain tests/*\nx.ru email-domain\n")
    r = _run(tmp_path)
    assert r.stdout.splitlines()[:-1] == ["src/s.py:1: email"]


def test_bad_allow_line_is_usage_error(tmp_path):
    _write(tmp_path, ".check-private-allow", "a b c\n")
    assert _run(tmp_path).returncode == 2


def test_symlink_and_big_file(tmp_path):
    _write(tmp_path, "data/big.bin", "x")
    (tmp_path / "data" / "big.bin").write_bytes(b"\0" * (cp.BIG_FILE + 1))
    (tmp_path / "link").symlink_to(tmp_path / "data")
    r = _run(tmp_path)
    assert r.returncode == 1
    assert sorted(r.stdout.splitlines()[:-1]) == ["data/big.bin:0: big-file", "link:0: symlink"]


def test_clean_tree_exits_zero(tmp_path):
    _write(tmp_path, "README.md", j("Пишите на owner", "@", "example.com, +7 900 000-00-01.\n"))
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout
    assert r.stdout.strip().endswith("0 finding(s)")
