"""Уведомления трекеров с почты (Ф10-5, часть A; `docs/architecture.md`, A1–A5).

Письмо Jira/Trello -> беседа на задачу/карточку (`jira:<домен>:<KEY>`, `trello:c:<short>`, `trello:b:<short>`)
вместо беседы-треда Gmail, текст — выжимка (Jira «обновление» — строки «Поле: было → стало»; Trello — действие
и текст комментария), отправитель — автор действия. Неизвестный шаблон — общая чистка (`clean_generic`).
**Никогда не терять текст:** выжимка пустая -> общая чистка; та пустая -> исходный текст; письмо, которое не
разобралось в беседу трекера, — обычное письмо (`route` -> `None`, прежний путь `convert.message_event`).

Чистые функции, без сети и без журнала с текстом писем. Ссылка на задачу строится хабом (из домена `From` и
проверенного ключа), из текста письма не копируется.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection
from dataclasses import dataclass, field

from .. import refs as refs_mod
from . import convert, textproc
from .imapwire import FetchedMsg

JIRA = "jira"
TRELLO = "trello"
MAX_NAME_LEN = 200  # название задачи/карточки, доска, имя автора

_LABEL = r"[a-z0-9-]+"
_JIRA_DOMAIN_RE = re.compile(rf"(?:{_LABEL}\.)+atlassian\.net")
_TRELLO_DOMAIN_RE = re.compile(rf"(?:{_LABEL}\.)*trello\.com")
_JIRA_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,19}-\d{1,7}")
_SUBJECT_KEY_RE = re.compile(r"\(([A-Z][A-Z0-9_]{1,19}-\d{1,7})\)")
_SUBJECT_TITLE_RE = re.compile(r"«(.+)»")
_TRELLO_LINK_RE = re.compile(r"https://trello\.com/([cb])/([A-Za-z0-9]{8})(?![A-Za-z0-9])")

# --- общая чистка (A4) -------------------------------------------------------------------------------------

_FOOTER_MARKERS = (
    "Просмотреть задачу",
    "View issue",
    "Подсказка. Обновляйте задачи",
    "Download on the App Store",
    "Это электронное письмо отправлено из Atlassian Jira",
    "This message was sent by Atlassian",
    "👀 Ознакомьтесь",
    "Чтобы настроить",
    "Получайте уведомления",
    "Это письмо от Trello",
)
_JUNK_LINES = frozenset(
    {"Логотип Jira", "Логотип Trello", "Avatar", "Atlassian logo", "ПОСЛЕДНИЕ УВЕДОМЛЕНИЯ..."}
)
_JUNK_HOSTS = frozenset(
    {
        "secure.gravatar.com",
        "trello-members.s3.amazonaws.com",
        "track.atlassian.com",
        "itunes.apple.com",
        "apps.apple.com",
        "play.google.com",
        "workspace.google.com",
        "marketplace.microsoft.com",
        "surveys.atlassian.com",
    }
)
_IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|svg|webp)(?=$|[?#&])", re.IGNORECASE)
_URL_TOKEN_RE = re.compile(r"\[([^\[\]\s]+)\]|(https?://[^\s\[\]<>]+)")
_TRELLO_TAIL_RE = re.compile(r"^https://trello\.com/([cb])/([A-Za-z0-9]{8})(?:[/?#]\S*)?$")
_URL_TRAIL = ".,;:!?\"'»"


def _is_footer_marker(line: str) -> bool:
    return line.strip().startswith(_FOOTER_MARKERS)


def _host_and_rest(url: str) -> tuple[str, str]:
    rest = url.split("://", 1)[1] if "://" in url else url
    host = re.split(r"[/?#]", rest, maxsplit=1)[0].lower()
    return host, rest[len(host) :]


def _junk_url(url: str) -> bool:
    host, rest = _host_and_rest(url)
    if _IMAGE_EXT_RE.search(url):
        return True
    if host in _JUNK_HOSTS:
        return True
    if host.startswith("post-office-rml-frontend") and host.endswith(".atl-paas.net"):
        return True
    return host == "www.atlassian.com" and rest.lower().startswith("/legal")


def _drop_query_param(url: str, name: str) -> str:
    base, sep, query = url.partition("?")
    if not sep:
        return url
    query, hsep, frag = query.partition("#")
    kept = [p for p in query.split("&") if p and p.split("=", 1)[0] != name]
    out = base + ("?" + "&".join(kept) if kept else "")
    return out + (hsep + frag if hsep else "")


def _tidy_url(url: str) -> str:
    host, _rest = _host_and_rest(url)
    if host.endswith((".atlassian.net", ".atlassian.com")) or host in ("atlassian.net", "atlassian.com"):
        url = _drop_query_param(url, "atlOrigin")
    m = _TRELLO_TAIL_RE.match(url)
    if m:
        url = f"https://trello.com/{m.group(1)}/{m.group(2)}"
    return url


def _peel(url: str) -> tuple[str, str]:
    """Хвостовая пунктуация — не часть голой ссылки (`…/a).` в тексте); `)` — только непарная."""
    tail = ""
    while url:
        ch = url[-1]
        if ch in _URL_TRAIL or (ch == ")" and url.count("(") < url.count(")")):
            tail = ch + tail
            url = url[:-1]
            continue
        break
    return url, tail


def _clean_urls(line: str) -> str:
    def _sub(m: re.Match) -> str:
        inner = m.group(1)
        if inner is not None:
            if "@boards.trello.com" in inner.lower():
                return ""
            if not inner.lower().startswith(("http://", "https://")):
                return m.group(0)
            if _junk_url(inner):
                return ""
            return f"[{_tidy_url(inner)}]"
        url, tail = _peel(m.group(2))
        if not url or _junk_url(url):
            return tail
        return _tidy_url(url) + tail

    return _URL_TOKEN_RE.sub(_sub, line)


def clean_generic(text: str) -> str:
    """A4: картинки, слежение, магазины приложений, подвал — прочь; `atlOrigin` у ссылок Atlassian прочь;
    ссылки Trello — до `https://trello.com/c|b/<short>`; обрезать с первой строки-маркера подвала; одинаковые
    соседние строки — одна. Результат пуст -> исходный текст."""
    source = text or ""
    out: list[str] = []
    for raw_line in html.unescape(source).split("\n"):
        line = _clean_urls(raw_line).strip()
        if _is_footer_marker(line):
            break
        if line in _JUNK_LINES:
            continue
        if line and out and out[-1] == line:
            continue
        out.append(line)
    result = textproc._tidy("\n".join(out))  # noqa: SLF001 — общая чистка строк того же пакета
    return result or source


# --- модель ---------------------------------------------------------------------------------------------


@dataclass
class Notification:
    """Одно уведомление об одной задаче/карточке. `action is None` — `body` уже готовый текст (Jira, общая
    чистка); иначе (Trello) текст — строка действия и под ней `body`."""

    tracker: str
    scope: str  # Jira — домен; Trello — "c" (карточка) | "b" (доска)
    key: str
    title: str | None
    actor: str | None
    body: str
    mentions_me: bool
    action: str | None = None
    board: str | None = None


@dataclass
class TrackerPart:
    """Событие письма в одну беседу трекера (`convert.tracker_event`)."""

    channel: dict
    tracker: str
    key: str
    url: str
    actor: str | None
    text: str
    mentions_me: bool
    refs: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)


def tracker_of_domain(domain: str) -> str | None:
    """A1: домен `From` -> `jira` (`*.atlassian.net` по границе метки) | `trello` (`trello.com`,
    `*.trello.com`) | `None`."""
    d = (domain or "").lower()
    if _JIRA_DOMAIN_RE.fullmatch(d):
        return JIRA
    if _TRELLO_DOMAIN_RE.fullmatch(d):
        return TRELLO
    return None


def tracker_of_address(address: str | None) -> str | None:
    return tracker_of_domain(convert.domain_of(address or ""))


def _cap(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value[:MAX_NAME_LEN] or None


# --- Jira (A2) ----------------------------------------------------------------------------------------------

_UPDATE_HEAD_RE = re.compile(r"^(?P<actor>.*?)Пользователь выполнил \d+ обновлен\w*$")
_FIELD_RE = re.compile(r"^(?P<field>[^:\[\]]{1,60}?) ?: (?P<rest>.*)$")
# Живые письма Jira (25.09): вокруг двоеточия и в тексте — неразрывные/узкие пробелы, бывают невидимые
# символы; образцы набраны обычными пробелами — разбор «обновления» на проде не срабатывал. Любой пробельный
# символ, кроме перевода строки, -> обычный пробел, невидимые — прочь.
_ANY_SPACE_RE = re.compile(r"[^\S\n]+")
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def _norm_spaces(line: str) -> str:
    return _ANY_SPACE_RE.sub(" ", _INVISIBLE_RE.sub("", line))
_IMG_BRACKET_RE = re.compile(
    r"\[https?://[^\]\s]*\.(?:png|jpe?g|gif|svg|webp)(?:[?#][^\]\s]*)?\]", re.IGNORECASE
)
_ARROW_RE = re.compile(r"\s*→\s*")
_JIRA_FOR_ME_RE = re.compile(r"упомянул|mentioned you|комментари|commented", re.IGNORECASE)
_JIRA_FROM_TAIL_RE = re.compile(r"\s*\(Jira\)\s*$", re.IGNORECASE)


def _browse_keys(line: str, domain: str) -> list[str]:
    """Ключи ссылок `https://<домен>/browse/<KEY>` строки, чей путь — ровно ключ (хвост `?…` не в счёт)."""
    pattern = re.compile(rf"https://{re.escape(domain)}/browse/([^\s?#\]\)\"'<>]+)", re.IGNORECASE)
    return [k for k in pattern.findall(line) if _JIRA_KEY_RE.fullmatch(k)]


def jira_link_keys(text: str, domain: str) -> list[str]:
    return [k for line in (text or "").split("\n") for k in _browse_keys(line, domain)]


def _jira_title_from_text(lines: list[str], key: str, domain: str) -> str | None:
    """Строка текста перед второй ссылкой `/browse/KEY` (не «Просмотреть задачу», не `Проект / KEY`)."""
    hits = [i for i, line in enumerate(lines) if key in _browse_keys(line, domain)]
    if len(hits) < 2:
        return None
    j = hits[1] - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0:
        return None
    cand = lines[j].strip()
    if cand.startswith(("[", "http://", "https://")) or _is_footer_marker(cand):
        return None
    if re.search(rf"/\s*{re.escape(key)}$", cand):
        return None
    return _cap(cand)


def _jira_update(lines: list[str]) -> tuple[str, list[str]] | None:
    """Шаблон «обновление»: автор (приклеен к «Пользователь выполнил N обновлений») и строки
    `Поле: было → стало`. `None` — это не «обновление»."""
    head = None
    for i, line in enumerate(lines):
        m = _UPDATE_HEAD_RE.match(_norm_spaces(line).strip())
        if m:
            head = (i, m.group("actor").strip())
            break
    if head is None:
        return None
    start, actor = head
    changes: list[str] = []
    cur_field: str | None = None
    cur_parts: list[str] = []

    def _flush() -> None:
        if cur_field is None:
            return
        value = _ARROW_RE.sub(" → ", " ".join(p for p in cur_parts if p)).strip()
        changes.append(f"{cur_field}: {value}")

    for line in lines[start + 1 :]:
        s = _norm_spaces(_IMG_BRACKET_RE.sub("", line)).strip()
        if _is_footer_marker(s):
            break
        if not s:
            continue
        fm = _FIELD_RE.match(s)
        if fm:
            _flush()
            cur_field = fm.group("field").strip()
            cur_parts = [fm.group("rest").strip()]
        elif cur_field is not None:
            cur_parts.append(s)
    _flush()
    return actor, changes


# --- Jira: письмо «обновления + комментарий» из HTML (Ф11-9b, `docs/architecture.md`) ---------
# У такого письма plain-часть — только текст комментария; шапка, «Обновления» и «Комментарии» — в HTML.
# HTML переводится в текст со служебными метками (символы из области частного использования — в тексте
# письма их не бывает): старое значение поля (`<span>` с `line-through`), стрелка (`<img alt="&#x2192;">`),
# строка автора (`<img alt="Avatar">`), граница комментария (кнопки «Ответить»/реакции) и подвал письма.

_M_OLD_ON = ""
_M_OLD_OFF = ""
_M_AUTHOR = ""
_M_CUT = ""
_M_ARROW = ""
_MARKS_RE = re.compile("[-]")
_OLD_SPAN_RE = re.compile(f"{_M_OLD_ON}([^{_M_OLD_OFF}]*){_M_OLD_OFF}")
_CUT_TESTIDS = frozenset({"comment-ctas", "jira-email-alt-footer"})
_HTML_UPDATES = frozenset({"Обновления", "Updates"})
_HTML_COMMENTS = frozenset({"Комментарии", "Comments"})
_HEAD_COMMENTS_RE = re.compile(
    r"добавил\w*\s+\d+\s+нов\w*\s+комментари|added\s+\d+\s+new\s+comments?", re.IGNORECASE
)
_HEAD_MENTION_RE = re.compile(r"упомянул|mentioned you", re.IGNORECASE)
_PLAIN_HEAD_RE = re.compile(
    r"Пользователь\s+(?:выполнил|добавил|упомянул)|made\s+\d+\s+updates?|added\s+\d+\s+new\s+comments?"
    r"|mentioned you",
    re.IGNORECASE,
)
_AUTHOR_TIME_RE = re.compile(r"^(?P<author>.+?)\s+\d{1,2}:\d{2}(?:\s.*)?$")
_VIEW_ISSUE_RE = re.compile(r"^(?:Просмотреть задачу|View issue)\s*\(https://")


class _JiraHtmlToText(textproc._HtmlToText):  # noqa: SLF001 — конвертер того же пакета, поведение не меняется
    """HTML Jira -> текст с метками. Картинка-стрелка -> метка стрелки, аватар -> метка строки автора, прочие
    картинки (реакции, логотипы, пиксель слежения) — ничего; ссылка без текста (кнопка-реакция) — ничего."""

    def __init__(self) -> None:
        super().__init__()
        self._spans: list[bool] = []

    def _img(self, attrs) -> None:
        alt = html.unescape(dict(attrs).get("alt") or "").strip()
        if alt == "→":
            self._emit(_M_ARROW)
        elif alt == "Avatar":
            self._emit(_M_AUTHOR)

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if self._skip_depth and t not in textproc._SKIP_TAGS:  # noqa: SLF001
            return
        if t == "img":
            self._img(attrs)
            return
        if t == "span":
            old = "line-through" in (dict(attrs).get("style") or "").replace(" ", "")
            self._spans.append(old)
            if old:
                self._emit(_M_OLD_ON)
            return
        super().handle_starttag(tag, attrs)
        if (dict(attrs).get("data-testid") or "") in _CUT_TESTIDS:
            self._newline()
            self._emit(_M_CUT)
            self._newline()

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() == "img":
            if not self._skip_depth:
                self._img(attrs)
            return
        super().handle_startendtag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if not self._skip_depth:
            if t == "span":
                if self._spans and self._spans.pop():
                    self._emit(_M_OLD_OFF)
                return
            if t == "a" and self._in_link and not "".join(self._link_text).strip():
                self._in_link = False
                self._link_href = None
                self._link_text = []
                return
        super().handle_endtag(tag)


def jira_html_text(markup: str) -> str:
    parser = _JiraHtmlToText()
    try:
        parser.feed(markup)
        parser.close()
    except AssertionError, ValueError:
        pass
    return parser.text()


def _unmark(s: str) -> str:
    return _MARKS_RE.sub("", s)


@dataclass
class _JiraHtml:
    head_comment: bool
    head_mention: bool
    changes: list[str]
    update_actor: str | None
    comments: list[tuple[str | None, str]]


def _html_author(line: str) -> str | None:
    s = _norm_spaces(_unmark(line)).strip()
    m = _AUTHOR_TIME_RE.match(s)
    return _cap(m.group("author") if m else s)


def _render_change(field_name: str, value: str) -> str:
    """`было → стало` (стрелка — картинка); без старого — `стало`; только зачёркнутое старое (значение
    снято) — `было → —`."""
    v = _norm_spaces(value).strip()
    if _M_ARROW in v:
        old, new = (" ".join(_unmark(x).split()) for x in v.split(_M_ARROW, 1))
        return f"{field_name}: {old} → {new}" if old else f"{field_name}: {new}"
    struck = " ".join(" ".join(m.group(1).split()) for m in _OLD_SPAN_RE.finditer(v)).strip()
    rest = " ".join(_unmark(_OLD_SPAN_RE.sub("", v)).split())
    if struck and not rest:
        return f"{field_name}: {struck} → —"
    return f"{field_name}: {' '.join(_unmark(v).split())}"


def _parse_jira_html(text: str, domain: str) -> _JiraHtml:
    """Разбор текста HTML-части (`jira_html_text`): шапка, строки «Обновления», тексты «Комментариев».
    Пустые `changes` и `comments` — HTML не разобрался (вызывающий идёт прежним путём)."""
    lines = text.split("\n")
    first_link = next((i for i, x in enumerate(lines) if f"{domain}/browse/" in x.lower()), len(lines))
    head = " ".join(_norm_spaces(_unmark(x)) for x in lines[:first_link])
    plain = [_norm_spaces(_unmark(x)).strip() for x in lines]
    upd_at = next((i for i, x in enumerate(plain) if x in _HTML_UPDATES), None)
    com_at = next(
        (i for i, x in enumerate(plain) if x in _HTML_COMMENTS and (upd_at is None or i > upd_at)), None
    )
    stop = next(
        (i for i, x in enumerate(lines) if x.strip() == _M_CUT or _VIEW_ISSUE_RE.match(plain[i])), len(lines)
    )

    # «Обновления»: блоки «автор время» + «Поле : было → стало»
    changes: list[str] = []
    update_actor: str | None = None
    if upd_at is not None or com_at is None:
        begin = upd_at + 1 if upd_at is not None else first_link
        end = com_at if com_at is not None else stop
        cur: tuple[str, list[str]] | None = None
        seen_author = False
        for raw_line in lines[begin:end]:
            s = raw_line.strip()
            if s.startswith(_M_AUTHOR):
                seen_author = True
                if update_actor is None:
                    update_actor = _html_author(s)
                continue
            if not seen_author or not _unmark(s).strip():
                continue
            fm = _FIELD_RE.match(_norm_spaces(s))
            if fm and _M_OLD_ON not in fm.group("field") and _M_ARROW not in fm.group("field"):
                if cur is not None:
                    changes.append(_render_change(cur[0], " ".join(cur[1])))
                cur = (fm.group("field").strip(), [fm.group("rest")])
            elif cur is not None:
                cur[1].append(s)
        if cur is not None:
            changes.append(_render_change(cur[0], " ".join(cur[1])))
        changes = [c for c in changes if c.split(": ", 1)[1].strip()]

    # «Комментарии»: «автор время» + текст до кнопок комментария / следующего автора / «Просмотреть задачу»
    comments: list[tuple[str | None, str]] = []
    if com_at is not None:
        author: str | None = None
        block: list[str] | None = None

        def _close() -> None:
            if block is not None:
                body = textproc._tidy("\n".join(_unmark(x) for x in block))  # noqa: SLF001
                if body:
                    comments.append((author, body))

        for i in range(com_at + 1, len(lines)):
            s = lines[i].strip()
            if s.startswith(_M_AUTHOR):
                _close()
                author, block = _html_author(s), []
            elif s == _M_CUT or _VIEW_ISSUE_RE.match(plain[i]):
                _close()
                block = None
            elif block is not None:
                block.append(lines[i])
        _close()
    return _JiraHtml(
        head_comment=bool(_HEAD_COMMENTS_RE.search(head)),
        head_mention=bool(_HEAD_MENTION_RE.search(head)),
        changes=changes,
        update_actor=update_actor,
        comments=comments,
    )


def plain_is_full_jira(text: str, subject: str | None, domain: str) -> bool:
    """plain письма Jira — полное уведомление (шапка «Пользователь …» или ссылки `/browse/` на задачу из
    темы). Иначе (письмо «обновления + комментарий») plain — только комментарий, разбирать HTML."""
    if _PLAIN_HEAD_RE.search(_norm_spaces(html.unescape(text or ""))):
        return True
    keys = jira_link_keys(html.unescape(text or ""), domain)
    if not keys:
        return False
    m = _SUBJECT_KEY_RE.search(subject or "")
    return m is None or m.group(1) in keys


def parse_jira(
    text: str,
    subject: str | None,
    from_domain: str,
    *,
    from_name: str | None = None,
    html_text: str | None = None,
) -> list[Notification] | None:
    """A2. `None` — ключа нет (обычное письмо). `html_text` — текст HTML-части (`jira_html_text`), когда
    plain — только комментарий (Ф11-9b): тогда выжимка из HTML, не разобрался — прежний путь по plain."""
    domain = (from_domain or "").lower()
    lines = html.unescape(text or "").split("\n")
    # plain «только комментарий» может ссылаться на другую задачу — ключ из HTML важнее
    keys = jira_link_keys(html_text, domain) if html_text else []
    keys = keys or [k for line in lines for k in _browse_keys(line, domain)]
    key = keys[0] if keys else None
    if key is None:
        m = _SUBJECT_KEY_RE.search(subject or "")
        key = m.group(1) if m else None
    if key is None:
        return None
    tm = _SUBJECT_TITLE_RE.search(subject or "")
    name = _cap(tm.group(1)) if tm else None
    if name is None:
        name = _jira_title_from_text(lines, key, domain)

    from_actor = _cap(_JIRA_FROM_TAIL_RE.sub("", from_name or "")) if from_name else None
    parsed = _parse_jira_html(html_text, domain) if html_text else None
    if parsed is not None and (parsed.changes or parsed.comments):
        actor = (parsed.comments[0][0] if parsed.comments else None) or parsed.update_actor or from_actor
        blocks = ["\n".join(parsed.changes)] if parsed.changes else []
        if len(parsed.comments) == 1:
            blocks.append(parsed.comments[0][1])
        else:
            blocks.extend(f"{a}: {t}" if a else t for a, t in parsed.comments)
        body = "\n\n".join(blocks)
        mentions = bool(parsed.comments) or parsed.head_comment or parsed.head_mention
        return [
            Notification(
                tracker=JIRA, scope=domain, key=key, title=name, actor=actor, body=body, mentions_me=mentions
            )
        ]

    update = _jira_update(lines)
    if update is not None and update[1]:
        actor = _cap(update[0]) or from_actor
        body = "\n".join(update[1])
        mentions = False
    else:
        actor = (_cap(update[0]) if update is not None else None) or from_actor
        body = clean_generic(text)
        mentions = bool(_JIRA_FOR_ME_RE.search(body))
    if parsed is not None and (parsed.head_comment or parsed.head_mention):
        mentions = True
    return [
        Notification(
            tracker=JIRA, scope=domain, key=key, title=name, actor=actor, body=body, mentions_me=mentions
        )
    ]


# --- Trello (A3) --------------------------------------------------------------------------------------------

_IMAGE_LINE_RE = re.compile(r"^\[https://\S+\.(?:png|jpe?g|gif|webp)\S*\]$", re.IGNORECASE)
_DASHES_RE = re.compile(r"^-{40,}$")
_REPLY_BY_MAIL = "Ответить по электронной почте"
_TRELLO_FOOTER_START = ("👀 Ознакомьтесь с нашими", "Это письмо от Trello •")
_TRELLO_FOR_ME_RE = re.compile(r"упомянул|прокомментировал|mentioned|commented", re.IGNORECASE)
_TRELLO_TEXT_FOR_ME_RE = re.compile(
    r"упомянул|прокомментировал|комментари|mentioned|commented", re.IGNORECASE
)


def _header_re(name: str) -> re.Pattern:
    return re.compile(
        rf"^Пользователь {re.escape(name)} (?P<verb>.*?карточк\w*) (?P<title>.+?) "
        r"\[https://trello\.com/c/(?P<short>[A-Za-z0-9]{8})[^\]]*\](?P<tail>.*?) на доске (?P<board>.+?) "
        r"\[https://trello\.com/b/(?P<bshort>[A-Za-z0-9]{8})[^\]]*\]$"
    )


def _trello_starts(lines: list[str]) -> list[tuple[int, int, str]]:
    """Начала уведомлений: `(индекс строки имени, индекс строки «Пользователь …», имя)`. Имя — ближайшая
    непустая строка выше, пропуская ОДНУ строку-картинку (аватар); строка «Пользователь <имя> …» должна
    начинаться этим именем — «Пользователь вводит…» в комментарии началом не считается."""
    out: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        if not line.strip().startswith("Пользователь "):
            continue
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and _IMAGE_LINE_RE.match(lines[j].strip()):
            j -= 1
            while j >= 0 and not lines[j].strip():
                j -= 1
        if j < 0:
            continue
        name = lines[j].strip()
        if not name or name.startswith("[") or _IMAGE_LINE_RE.match(name):
            continue
        if not line.strip().startswith(f"Пользователь {name} "):
            continue
        out.append((j, i, name))
    return out


def _body_end(lines: list[str], begin: int, limit: int) -> int:
    """Конец тела: строка ровно «Ответить по электронной почте» со ссылкой `@boards.trello.com` следом;
    строка из ≥ 40 `-`, за которой в пределах 3 непустых строк маркер подвала; строка подвала, которой в
    комментарии не бывает (`_TRELLO_FOOTER_START`); начало следующего уведомления (`limit`). Прочие маркеры
    подвала («Чтобы настроить…», «Получайте уведомления…») сами тело НЕ обрывают — ими может начинаться
    строка комментария (ревью Ф10-5, находка 1)."""
    for k in range(begin, limit):
        s = lines[k].strip()
        if s == _REPLY_BY_MAIL and k + 1 < len(lines) and "@boards.trello.com" in lines[k + 1]:
            return k
        if s.startswith(_TRELLO_FOOTER_START):
            return k
        if _DASHES_RE.match(s):
            following = [x.strip() for x in lines[k + 1 :] if x.strip()][:3]
            if any(_is_footer_marker(x) for x in following):
                return k
    return limit


def _strip_blank_edges(block: list[str]) -> str:
    while block and not block[0].strip():
        block = block[1:]
    while block and not block[-1].strip():
        block = block[:-1]
    return "\n".join(x.strip() for x in block)


def _first_trello_key(text: str) -> tuple[str, str] | None:
    links = _TRELLO_LINK_RE.findall(text)
    for scope in ("c", "b"):
        for s, short in links:
            if s == scope:
                return scope, short
    return None


def _trello_fallback(block_text: str, actor: str | None) -> Notification | None:
    found = _first_trello_key(block_text)
    if found is None:
        return None
    body = clean_generic(block_text)
    return Notification(
        tracker=TRELLO,
        scope=found[0],
        key=found[1],
        title=None,
        actor=actor,
        body=body,
        mentions_me=bool(_TRELLO_TEXT_FOR_ME_RE.search(body)),
    )


def parse_trello(text: str) -> list[Notification] | None:
    """A3. `None` — письмо не раскладывается по карточкам (обычное письмо)."""
    lines = html.unescape(text or "").split("\n")
    starts = _trello_starts(lines)
    if not starts:
        note = _trello_fallback(text or "", None)
        return [note] if note is not None else None

    notes: list[Notification] = []
    for idx, (_name_at, at, name) in enumerate(starts):
        limit = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        header_end = next((k for k in range(at, limit) if "[https://trello.com/b/" in lines[k]), None)
        m = None
        if header_end is not None:
            header = " ".join(x.strip() for x in lines[at : header_end + 1] if x.strip())
            m = _header_re(name).match(header)
        if m is None:
            end = _body_end(lines, at + 1, limit)
            note = _trello_fallback("\n".join(lines[at:end]), _cap(name))
            if note is None:
                return None  # блок без карточки и доски — письмо целиком обычное (текст не теряется)
            notes.append(note)
            continue
        assert header_end is not None  # noqa: S101 — m не None только при найденной шапке
        end = _body_end(lines, header_end + 1, limit)
        action = " ".join(f"{m.group('verb')}{m.group('tail')}".split())
        notes.append(
            Notification(
                tracker=TRELLO,
                scope="c",
                key=m.group("short"),
                title=_cap(m.group("title")),
                actor=_cap(name),
                body=_strip_blank_edges(lines[header_end + 1 : end]),
                mentions_me=bool(_TRELLO_FOR_ME_RE.search(action)),
                action=action,
                board=_cap(m.group("board")),
            )
        )
    return notes or None


# --- маршрутизация (A5) ----------------------------------------------------------------------------------


def channel_ext_id(note: Notification) -> str:
    if note.tracker == JIRA:
        return f"jira:{note.scope}:{note.key}"
    return f"trello:{note.scope}:{note.key}"


def channel_kind(ext_id: str) -> str:
    return "issue" if ext_id.startswith("jira:") else "card"


def is_tracker_channel(ext_id: str | None) -> bool:
    return bool(ext_id) and str(ext_id).startswith(("jira:", "trello:"))


def _url(note: Notification) -> str:
    if note.tracker == JIRA:
        return f"https://{note.scope}/browse/{note.key}"
    return f"https://trello.com/{note.scope}/{note.key}"


def _compose(notes: list[Notification], sender: str | None) -> str:
    blocks: list[str] = []
    for n in notes:
        if n.action is None:
            block = n.body
        else:
            head = n.action if (not n.actor or n.actor == sender) else f"{n.actor} {n.action}"
            block = head + (f"\n{n.body}" if n.body else "")
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def route(
    msg: FetchedMsg,
    *,
    thread_msgs: list[FetchedMsg],
    known: Collection[str] = (),
) -> list[TrackerPart] | None:
    """A1 + A5: письмо трекера -> по части на каждую беседу задачи/карточки (в порядке письма); `None` —
    обычное письмо (прежний путь). `thread_msgs` — метаданные треда Gmail (`attrs` раскладки), `known` —
    `ext_id` бесед, уже известных ядру (заголовок «KEY» без названия шлётся только новой беседе)."""
    raw = msg.body or b""
    from_list = convert.addresses(raw, "From")
    if not from_list:
        return None
    from_name, from_addr = from_list[0]
    domain = convert.domain_of(from_addr)
    tracker = tracker_of_domain(domain)
    if tracker is None:
        return None
    body = textproc.extract_body(raw, partial=msg.body_partial, strip_quote=False)
    if body.unparsed or not body.text:
        return None
    text = body.text
    if tracker == JIRA:
        subject = convert.decoded_header(raw, "Subject") or ""
        html_text = None
        if not plain_is_full_jira(text, subject, domain):
            markup = textproc.extract_html(raw)
            html_text = jira_html_text(markup) if markup else None
        if not (
            subject.lower().startswith("[jira]")
            or jira_link_keys(text, domain)
            or (html_text and jira_link_keys(html_text, domain))
        ):
            return None
        notes = parse_jira(text, subject, domain, from_name=from_name, html_text=html_text)
    else:
        if not _TRELLO_LINK_RE.search(text):
            return None
        notes = parse_trello(text)
    if not notes:
        return None

    groups: dict[str, list[Notification]] = {}
    for n in notes:
        groups.setdefault(channel_ext_id(n), []).append(n)
    parts: list[TrackerPart] = []
    for ext_id, group in groups.items():
        first = group[0]
        url = _url(first)
        name = next((n.title for n in group if n.title), None)
        board = next((n.board for n in group if n.board), None)
        if first.tracker == JIRA:
            title = f"{first.key} · {name}" if name else (first.key if ext_id not in known else None)
        else:
            title = name
        sender = first.actor
        text_out = _compose(group, sender) or clean_generic(text)
        extra: dict = {"tracker": first.tracker, "key": first.key, "url": url, "no_reply": True}
        if first.tracker == TRELLO and board:
            extra["board"] = board
        channel: dict = {"ext_id": ext_id, "kind": channel_kind(ext_id), "own_id_space": True}
        if title is not None:
            channel["title"] = title
        channel["attrs"] = convert.thread_attrs(thread_msgs, extra=extra)
        first_ref = first.key if first.tracker == JIRA else url
        refs = [first_ref]
        for r in refs_mod.extract_refs(text_out):
            if r not in refs:
                refs.append(r)
        parts.append(
            TrackerPart(
                channel=channel,
                tracker=first.tracker,
                key=first.key,
                url=url,
                actor=sender,
                text=text_out,
                mentions_me=any(n.mentions_me for n in group),
                refs=refs[: refs_mod.MAX_REFS],
                attachments=list(body.attachments),
            )
        )
    return parts
