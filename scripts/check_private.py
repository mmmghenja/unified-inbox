#!/usr/bin/env python3
"""Generic privacy checker for this repository: finds things that must not be published.

    python3 scripts/check_private.py [DIR]        (or scripts/check-private.sh [DIR])

Rules (id — what):
    email          address on a non-reserved domain (allowed: example.com/.org/.net and their subdomains,
                   the .test / .example / .invalid / .localhost TLDs, plus domains from the allow file)
    phone          Russian mobile number (+7/8 9xx …) other than the test range +7 900 0XX-XX-XX,
                   or an E.164 number of 11+ digits
    private-ip     10.x, 172.16-31.x, 192.168.x addresses
    slack-token    xoxb-/xoxp-/xoxa-/xoxs-/xoxr-…, xapp-…
    api-key        sk-… (OpenAI/Anthropic style), AKIA… (AWS), AIza… (Google), gh?_… (GitHub)
    bot-token      Telegram bot token (digits:AA…)
    private-key    PEM private key block, AGE-SECRET-KEY
    api-hash       Telegram api_hash = 32 hex
    inn, ogrn      Russian tax/registration numbers next to their keyword, checksum-valid
    passport       Russian passport series and number next to the keyword
    card           payment card number (Luhn-valid, grouped, or next to a card keyword)
    big-file       file larger than 1 MiB (lock files excepted)
    symlink        symbolic link

The matched value is never printed — only `path:line: rule`. Exit code 0 — clean, 1 — findings, 2 — usage.

Allowing a finding:
    * same line, or a comment line right above it, contains `showcase-ok:` followed by a reason;
    * `.check-private-allow` in the scanned root: lines `<glob> <rule>[,<rule>…]  # reason`
      (glob against the path relative to the root, `*` crosses `/`), or `<domain> email-domain [<glob>]` —
      addresses on that domain and its subdomains are allowed everywhere or only under the glob.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

BIG_FILE = 1024 * 1024
SKIP_DIRS = {".git", "node_modules", ".venv", "dist", "__pycache__", ".pytest_cache", ".ruff_cache"}
LOCK_FILES = {"uv.lock", "package-lock.json"}
MARK = "showcase-ok:"

RESERVED_TLDS = (".test", ".example", ".invalid", ".localhost")
RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
# `name@instance.service` is a systemd unit instance, not an address.
UNIT_SUFFIXES = (".service", ".timer", ".socket", ".target", ".mount", ".path", ".slice")

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![\w-])")
PHONE_RU_RE = re.compile(
    r"(?<![\w+])(?:\+7|7|8)[ \-(]{0,2}9\d\d[ \-)]{0,2}\d\d\d[ -]?\d\d[ -]?\d\d(?!\d)"
)
E164_RE = re.compile(r"(?<![\w+])\+\d{11,15}(?!\d)")
SAFE_PHONE = re.compile(r"79000\d{6}")
IP_RE = re.compile(
    r"(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}(?![\d.])"
)
SIMPLE_RULES: dict[str, re.Pattern[str]] = {
    "slack-token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}|\bxapp-\d-[A-Za-z0-9-]{10,}"),
    "api-key": re.compile(
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{35}\b"
        r"|\bgh[pousr]_[A-Za-z0-9]{36}\b"
    ),
    "bot-token": re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----|AGE-SECRET-KEY-1[0-9A-Z]{20,}"),
    "api-hash": re.compile(r"(?i)api_hash\W{0,4}[0-9a-f]{32}\b"),
}
INN_RE = re.compile(r"(?i)(?:инн|\binn)\D{0,6}(\d{12}|\d{10})(?!\d)")
OGRN_RE = re.compile(r"(?i)(?:огрн(?:ип)?|\bogrn)\D{0,6}(\d{15}|\d{13})(?!\d)")
PASSPORT_RE = re.compile(r"(?i)(?:паспорт|passport)\D{0,12}\d{2}\s?\d{2}\s?\d{6}(?!\d)")
CARD_GROUPED_RE = re.compile(r"(?<![\d-])(\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4})(?![\d-])")
CARD_PLAIN_RE = re.compile(r"(?i)(?:card|карт)\w{0,8}\D{0,12}(?<!\d)(\d{16})(?!\d)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _inn_ok(s: str) -> bool:
    d = [int(c) for c in s]

    def ctl(weights: list[int], n: int) -> int:
        return sum(w * d[i] for i, w in enumerate(weights)) % 11 % 10 == d[n]

    if len(d) == 10:
        return ctl([2, 4, 10, 3, 5, 9, 4, 6, 8], 9)
    return ctl([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 10) and ctl([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], 11)


def _ogrn_ok(s: str) -> bool:
    body, check = int(s[:-1]), int(s[-1])
    mod = 11 if len(s) == 13 else 13
    return body % mod % 10 == check


def _email_allowed(domain: str, extra: set[str]) -> bool:
    d = domain.lower().rstrip(".")
    if d.endswith(UNIT_SUFFIXES):
        return True
    if d.endswith(RESERVED_TLDS) or d in RESERVED_TLDS or d == "localhost":
        return True
    if any(d == r or d.endswith("." + r) for r in RESERVED_DOMAINS):
        return True
    return any(d == a or d.endswith("." + a) for a in extra)


def line_findings(line: str, extra_domains: set[str]) -> list[str]:
    """Rule ids that fire on one line (each id once)."""
    hits: list[str] = []
    if any(not _email_allowed(m.group(1), extra_domains) for m in EMAIL_RE.finditer(line)):
        hits.append("email")
    phones = [re.sub(r"\D", "", m.group(0)) for m in PHONE_RU_RE.finditer(line)]
    phones += [m.group(0)[1:] for m in E164_RE.finditer(line)]
    if any(not SAFE_PHONE.fullmatch("7" + p[1:] if len(p) == 11 else p) for p in phones):
        hits.append("phone")
    if IP_RE.search(line):
        hits.append("private-ip")
    hits += [rid for rid, rx in SIMPLE_RULES.items() if rx.search(line)]
    if any(_inn_ok(m.group(1)) for m in INN_RE.finditer(line)):
        hits.append("inn")
    if any(_ogrn_ok(m.group(1)) for m in OGRN_RE.finditer(line)):
        hits.append("ogrn")
    if PASSPORT_RE.search(line):
        hits.append("passport")
    cards = [re.sub(r"\D", "", m.group(1)) for m in CARD_GROUPED_RE.finditer(line)]
    cards += [m.group(1) for m in CARD_PLAIN_RE.finditer(line)]
    if any(_luhn(c) for c in cards):
        hits.append("card")
    return hits


def load_allow(root: Path) -> tuple[list[tuple[str, set[str]]], list[tuple[str, str]]]:
    """`.check-private-allow`: `<glob> <rules>` lines, and `<domain> email-domain [<glob>]` lines (domain
    allowed everywhere, or only in files under the glob; glob `*` = everywhere)."""
    entries: list[tuple[str, set[str]]] = []
    domains: list[tuple[str, str]] = []
    path = root / ".check-private-allow"
    if not path.is_file():
        return entries, domains
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[1:2] == ["email-domain"] and len(parts) in (2, 3):
            domains.append((parts[0].lower(), parts[2] if len(parts) == 3 else "*"))
        elif len(parts) == 2 and "email-domain" not in parts[1].split(","):
            entries.append((parts[0], set(parts[1].split(","))))
        else:
            raise ValueError(f".check-private-allow: bad line: {raw!r}")
    return entries, domains


def _allowed_by_file(rel: str, rule: str, entries: list[tuple[str, set[str]]]) -> bool:
    return any(fnmatch.fnmatchcase(rel, g) and (rule in rules or "*" in rules) for g, rules in entries)


def scan(root: Path) -> list[str]:
    entries, domains = load_allow(root)
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(p in SKIP_DIRS for p in rel_parts[:-1]) or (rel_parts and rel_parts[0] in SKIP_DIRS):
            continue
        rel = "/".join(rel_parts)
        if path.is_symlink():
            if not _allowed_by_file(rel, "symlink", entries):
                found.append(f"{rel}:0: symlink")
            continue
        if not path.is_file() or rel == ".check-private-allow":
            continue
        if path.name in LOCK_FILES:
            continue
        size = path.stat().st_size
        if size > BIG_FILE and not _allowed_by_file(rel, "big-file", entries):
            found.append(f"{rel}:0: big-file")
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        here = {d for d, g in domains if fnmatch.fnmatchcase(rel, g)}
        for i, line in enumerate(lines):
            hits = line_findings(line, here)
            if not hits:
                continue
            above = lines[i - 1].strip() if i else ""
            if MARK in line or (MARK in above and above.startswith(("#", "//", "/*", "*", "<!--", "--"))):
                continue
            for rule in hits:
                if not _allowed_by_file(rel, rule, entries):
                    found.append(f"{rel}:{i + 1}: {rule}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and not Path(argv[1]).is_dir()):
        print("usage: check_private.py [DIR]", file=sys.stderr)
        return 2
    root = Path(argv[1] if len(argv) == 2 else ".")
    try:
        found = scan(root)
    except ValueError as exc:
        print(f"check-private: {exc}", file=sys.stderr)
        return 2
    for line in found:
        print(line)
    print(f"check-private: {len(found)} finding(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
