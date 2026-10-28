"""Данные демо-стенда. Всё выдумано: компании Acme, Globex, Initech, Umbrella, люди с вымышленными именами,
адреса только на example.com. Ни одного настоящего имени, домена или номера.

Модуль только описывает и засевает мир (`seed_world`); живой трафик и запуск ядра — в `demo/serve.py`.
Ядро видит «Telegram» и «почту», а события толкает fake-коннектор: тот же путь `CoreClient.push_events`, что у
настоящих коннекторов, без обхода защиты ядра.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from hub.connectors.base import CoreClient

_logger = logging.getLogger("demo.seed")

MINUTE = 60_000

PROJECTS = ("Acme Storefront", "Globex Support", "Initech Billing", "Personal")

# (ext_id, kind, заголовок, проект или None = «Неразобранное», строки (кто, текст, минут назад); кто=None — я)
Line = tuple[str | None, str, int]
Chat = tuple[str, str, str, str | None, list[Line]]

TELEGRAM_CHATS: list[Chat] = [
    (
        "dm-dana",
        "dm",
        "Dana Whitfield",
        "Acme Storefront",
        [
            ("Dana Whitfield", "Morning! Did the staging deploy go through?", 190),
            (None, "Yes, green since 8:40. The checkout flow looks fine.", 184),
            ("Dana Whitfield", "Great. Marketing wants the discount banner live by Friday.", 95),
            (None, "Friday works if the coupon limits are confirmed.", 90),
            ("Dana Whitfield", "Can you check the limits with Globex before we announce anything?", 14),
        ],
    ),
    (
        "grp-launch",
        "group",
        "Acme: launch team",
        "Acme Storefront",
        [
            ("Sam Okafor", "Release candidate 3 is tagged.", 300),
            ("Lena Park", "QA found one issue: the cart badge shows 0 after login.", 240),
            (None, "Reproduced, fixing it now.", 233),
            ("Lena Park", "Thanks! Ping me when the build is up.", 230),
            ("Sam Okafor", "Reminder: freeze starts at 18:00.", 70),
            ("Lena Park", "Build 1482 is on staging, retesting the badge.", 25),
        ],
    ),
    (
        "dm-marcus",
        "dm",
        "Marcus Chen",
        "Globex Support",
        [
            ("Marcus Chen", "Hi! Our agents report that password reset emails arrive late.", 420),
            (None, "Thanks for flagging. Checking the mail queue now.", 410),
            (None, "The queue backed up between 09:10 and 09:40, it is clear now.", 380),
            ("Marcus Chen", "Perfect, can you send a short note I can forward to the team?", 60),
        ],
    ),
    (
        "chan-initech-notes",
        "channel",
        "Initech Release Notes",
        "Initech Billing",
        [
            ("Initech Release Notes", "Invoice export now supports CSV and XLSX.", 1500),
            ("Initech Release Notes", "Scheduled maintenance on Sunday, 02:00-03:00 UTC.", 600),
            ("Initech Release Notes", "New: recurring invoices can be paused for a period.", 45),
        ],
    ),
    (
        "dm-priya",
        "dm",
        "Priya Raman",
        None,
        [("Priya Raman", "Hello! We met at the meetup, could you share the slides?", 150)],
    ),
]

MAIL_CHATS: list[Chat] = [
    (
        "mail-renewal",
        "mail_thread",
        "Re: Q4 renewal terms",
        "Globex Support",
        [
            ("Tom Alvarez <legal@globex.example.com>", "Hi, attached is the redline of the renewal.", 2000),
            (None, "Thanks Tom. We accept sections 1-3 and propose a 60-day notice in section 4.", 1900),
            ("Tom Alvarez <legal@globex.example.com>", "Fine from our side, I will update the draft today.", 120),
        ],
    ),
    (
        "mail-invoice",
        "mail_thread",
        "Invoice #4821 is overdue",
        "Initech Billing",
        [
            ("Billing <ar@initech.example.com>", "Our records show invoice #4821 is 14 days overdue.", 800),
            ("Billing <ar@initech.example.com>", "Please confirm the payment date or reply with questions.", 55),
        ],
    ),
    (
        "mail-portal",
        "mail_thread",
        "Welcome to the Acme partner portal",
        "Acme Storefront",
        [("Acme Portal <no-reply@acme.example.com>", "Your account is ready. Sign in to view your orders.", 3000)],
    ),
    (
        "mail-digest",
        "mail_thread",
        "Umbrella product digest, week 43",
        None,
        [("Umbrella News <news@umbrella.example.com>", "This week: three launches and a webinar.", 500)],
    ),
]

BOT_CHAT: Chat = ("bot-deploy", "dm", "Acme Deploy Bot", "Acme Storefront", [])

FILLER_LINES = {
    "dm-dana": ("Dana Whitfield", ["Quick question about the banner copy.", "Can we sync at 15:00?", "Thanks!"]),
    "grp-launch": ("Lena Park", ["Staging is stable.", "One more edge case on the coupon field.", "LGTM"]),
    "dm-marcus": ("Marcus Chen", ["Any update on the reset emails?", "Our agents confirm it works now."]),
    "mail-invoice": ("Billing <ar@initech.example.com>", ["Payment received, thank you.", "Please resend the PO."]),
}


def now_ms() -> int:
    return int(time.time() * 1000)


def uid() -> str:
    return uuid.uuid4().hex


def name_and_id(who: str) -> tuple[str, str]:
    """`Имя <адрес>` -> (имя, адрес); просто имя -> (имя, имя без пробелов)."""
    if "<" in who and who.endswith(">"):
        name, _, addr = who.partition("<")
        return name.strip(), addr[:-1]
    return who, who.lower().replace(" ", "-")


def event(chat: Chat, line: Line, ts: int, ext_id: str) -> dict:
    """Сырое событие `upsert`; той же формы, что у настоящих коннекторов (`core/events.py`)."""
    ext, kind, title, _project, _lines = chat
    who, text, _ago = line
    ev = {
        "type": "upsert",
        "channel": {"ext_id": ext, "kind": kind, "title": title},
        "ext_id": ext_id,
        "ts": ts,
        "is_me": who is None,
        "kind": "text",
        "text": text,
        "mentions_me": False,
        "sender": None,
    }
    if who is not None:
        name, sender_id = name_and_id(who)
        ev["sender"] = {"ext_id": sender_id, "name": name}
    return ev


def chat_events(chat: Chat, base: int) -> list[dict]:
    """События чата от старых к новым; ext_id детерминирован — повторный засев ядро не задвоит."""
    lines = sorted(chat[4], key=lambda ln: -ln[2])
    return [event(chat, ln, base - ln[2] * MINUTE, f"{chat[0]}-{i + 1}") for i, ln in enumerate(lines)]


@dataclass
class DemoState:
    """Что нужно после засева; переживает перезапуск контейнера (`/data/demo-state.json`)."""

    tg_id: int = 0
    tg_secret: str = ""
    mail_id: int = 0
    mail_secret: str = ""
    host_token: str = ""
    projects: dict[str, int] = field(default_factory=dict)
    running_session: int = 0
    idle_session: int = 0
    work_root: str = ""


async def _post(client: httpx.AsyncClient, url: str, **kw) -> dict:
    r = await client.post(url, **kw)
    r.raise_for_status()
    return r.json()


async def _create_account(admin: httpx.AsyncClient, *, source: str, title: str, db: str) -> tuple[int, str]:
    body = await _post(admin, "/admin/accounts", json={"source": source, "title": title, "db": db, "config": None})
    return body["account_id"], body["connector_secret"]


async def _create_token(admin: httpx.AsyncClient, *, name: str, kind: str, scopes: list[str]) -> str:
    body = {"name": name, "kind": kind, "scopes": scopes, "project_ids": None}
    return (await _post(admin, "/admin/tokens", json=body))["token"]


async def name_machines(admin: httpx.AsyncClient, name: str = "demo") -> None:
    """Имя машины в реестре — `hostname` контейнера по умолчанию; в демо оно не должно быть случайным хешем."""
    r = await admin.get("/admin/machines")
    r.raise_for_status()
    for row in r.json().get("items") or []:
        if row.get("name") != name:
            rr = await admin.patch(f"/admin/machines/{row['host']}", json={"name": name})
            if rr.status_code not in (200, 404, 422):
                rr.raise_for_status()


async def login_code(admin: httpx.AsyncClient) -> str:
    return (await _post(admin, "/admin/login-code"))["code"]


def _bot_events(base: int) -> list[dict]:
    """Личка с ботом: под сообщением инлайн-кнопки всех видов (действие, ссылка, копирование)."""

    def bot(ext_id: str, text: str, ts: int, rows: list) -> dict:
        ev = event(BOT_CHAT, ("Acme Deploy Bot", text, 0), ts, ext_id)
        ev["sender"]["is_bot"] = True
        ev["buttons"] = {"kind": "inline", "rows": rows}
        ev["buttons_ts"] = ts
        return ev

    return [
        bot(
            "7001",
            "Build 1482 passed all checks. What next?",
            base - 40 * MINUTE,
            [
                [
                    {"t": "Approve release", "k": "callback", "d": "YXBwcm92ZTox"},
                    {"t": "Open dashboard", "k": "url", "u": "https://example.com/deploys/1482"},
                ],
                [{"t": "Copy build id", "k": "copy", "c": "build-1482"}],
            ],
        ),
        bot(
            "7002",
            "Nightly report is ready.",
            base - 20 * MINUTE,
            [[{"t": "Download report", "k": "callback", "d": "cmVwb3J0OjE="}]],
        ),
    ]


def _extras(base: int) -> list[dict]:
    """Ответ-цитата, реакция на сообщение лички и просмотры постов канала."""
    dana = TELEGRAM_CHATS[0]
    quoted = event(dana, (None, "Sending the coupon list in a minute.", 0), base - 10 * MINUTE, "dm-dana-reply")
    quoted["reply_to_ext_id"] = "dm-dana-3"
    reaction = {
        "type": "reaction",
        "channel": {"ext_id": "dm-dana", "kind": "dm"},
        "ext_id": "dm-dana-1",
        "reactions": [{"emoji": "👍", "count": 2, "me": True, "by": ["Dana Whitfield", "Sam Okafor"]}],
    }
    notes = TELEGRAM_CHATS[3]
    counters = [(1234, 5), (5870, 12), (940, None)]
    posts = []
    for i, (views, forwards) in enumerate(counters):
        post = event(notes, notes[4][i], base - notes[4][i][2] * MINUTE, f"{notes[0]}-{i + 1}")
        post["views"], post["forwards"] = views, forwards
        posts.append(post)
    return [quoted, reaction, *posts]


async def seed_world(
    admin: httpx.AsyncClient, public: httpx.AsyncClient, *, socket_path: Path, port: int, work_root: Path
) -> tuple[DemoState, CoreClient, CoreClient]:
    """Первый запуск: проекты, два fake-аккаунта (Telegram и почта), чаты, раскладка, черновик и карточки,
    сессии хаба. -> (состояние, клиент Telegram, клиент почты)."""
    st = DemoState(work_root=str(work_root))
    for name in PROJECTS:
        st.projects[name] = (await _post(admin, "/admin/projects", json={"name": name}))["project_id"]
    st.tg_id, st.tg_secret = await _create_account(admin, source="telegram", title="Acme Telegram", db="main")
    st.mail_id, st.mail_secret = await _create_account(admin, source="mail", title="Acme Mail", db="work")
    tg = CoreClient(socket_path=str(socket_path), secret=st.tg_secret)
    mail = CoreClient(socket_path=str(socket_path), secret=st.mail_secret)

    base = now_ms()
    for chat in TELEGRAM_CHATS:
        await tg.push_events(chat_events(chat, base))
    for chat in MAIL_CHATS:
        await mail.push_events(chat_events(chat, base))
    await tg.push_events([*_bot_events(base), *_extras(base)])

    origin = f"http://127.0.0.1:{port}"
    r = await public.post("/api/v1/auth/session", json={"code": await login_code(admin)}, headers={"Origin": origin})
    r.raise_for_status()
    headers = {"Origin": origin, "X-Hub-CSRF": r.json()["csrf"]}

    r = await public.get("/api/v1/projects/-1/channels")
    r.raise_for_status()
    by_ext = {c["ext_id"]: c["id"] for c in r.json()}
    for chat in (*TELEGRAM_CHATS, *MAIL_CHATS, BOT_CHAT):
        if chat[3] is not None:
            body = {"project_id": st.projects[chat[3]]}
            rr = await public.patch(f"/api/v1/channels/{by_ext[chat[0]]}", json=body, headers=headers)
            rr.raise_for_status()

    await _agent_cards(admin, public, port=port, by_ext=by_ext)
    await _seed_sessions(admin, public, st, port=port, headers=headers)
    return st, tg, mail


async def _agent_cards(admin: httpx.AsyncClient, public: httpx.AsyncClient, *, port: int, by_ext: dict) -> None:
    """Черновик от агента (ждёт подтверждения) и сессия, просящая нажать кнопку бота (карточка «Нажать?»)."""
    base_url = f"http://127.0.0.1:{port}"
    agent = await _create_token(admin, name="agent-demo", kind="interactive", scopes=["send"])
    async with httpx.AsyncClient(base_url=base_url) as client:  # без cookie: bearer и cookie вместе — 403
        await _post(
            client,
            "/api/v1/messages/send",
            json={
                "channel_id": by_ext["dm-marcus"],
                "text": "Hi Marcus, the reset emails were delayed by a queue backlog between 09:10 and 09:40. "
                "It is resolved, no action needed on your side.",
                "idempotency_key": uid(),
            },
            headers={"Authorization": f"Bearer {agent}"},
        )
    presser = await _create_token(admin, name="press-demo", kind="interactive", scopes=["draft"])
    r = await public.get(f"/api/v1/channels/{by_ext['bot-deploy']}/messages", params={"limit": 20})
    r.raise_for_status()
    report = next((m for m in r.json()["messages"] if m.get("ext_id") == "7002"), None)
    if report is None:
        _logger.warning("demo: сообщение бота не найдено — карточка нажатия не создана")
        return
    async with httpx.AsyncClient(base_url=base_url) as client:
        r = await client.post(
            f"/api/v1/messages/{report['id']}/buttons/press",
            json={
                "row": 1,
                "col": 1,
                "text": "Download report",
                "session": {"pid": 424242, "proc_start": 1, "session_id": "demo-press-session"},
            },
            headers={"Authorization": f"Bearer {presser}"},
        )
        if r.status_code not in (200, 201):
            _logger.warning("demo: нажатие сессии не создалось: %s", r.status_code)


# Процесса у демо-сессий нет: ядро хранит pid и время старта, но сам процесс не проверяет.
_PROC = {"proc_pid": 4242, "proc_start": 777_000}

LIVE_EVENTS = (
    {"kind": "tool", "tool": "Read", "summary": "src/cart/badge.py"},
    {"kind": "tool_done", "summary": "done (3120 chars)"},
    {"kind": "text", "summary": "The badge reads the count before the login merge finishes. Checking the order."},
    {
        "kind": "text",
        "summary": (
            "## Finding\n\nThe cart is merged **after** the badge renders:\n\n"
            "- `render_badge()` runs in `on_login`\n- `merge_cart()` runs in `on_session_ready`\n\n"
            "```python\nawait merge_cart(user)\nrender_badge(user)\n```\n\n"
            "| Step | Before | After |\n|---|---|---|\n| badge | 0 | 3 |"
        ),
    },
    {"kind": "tool", "tool": "Bash", "summary": "uv run pytest -q tests/test_cart.py -k badge"},
    {"kind": "tool_done", "summary": "error (212 chars): FAILED test_badge_after_login", "is_error": True},
    {"kind": "tool", "tool": "Grep", "summary": "src - merge_cart\\("},
    {"kind": "tool_done", "summary": "done (380 chars)"},
)


async def host_events(host: httpx.AsyncClient, session_id: int, events: list[dict]) -> None:
    r = await host.post(f"/api/v1/work/host/sessions/{session_id}/events", json={"events": events})
    r.raise_for_status()


async def host_state(host: httpx.AsyncClient, session_id: int, state: str) -> None:
    """Как хост: «процесс жив» (иначе ядро усыпит сессию без подтверждения)."""
    r = await host.post(f"/api/v1/work/host/sessions/{session_id}/state", json={"state": state, **_PROC})
    r.raise_for_status()


def _workdirs(root: Path) -> dict[str, Path]:
    """Пустые рабочие папки проектов с сессиями: ядро требует существующую папку."""
    folders = {name: root / name.split()[0].lower() for name in ("Acme Storefront", "Globex Support")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    return folders


async def _seed_sessions(
    admin: httpx.AsyncClient, public: httpx.AsyncClient, st: DemoState, *, port: int, headers: dict
) -> None:
    """Две сессии хаба: одна работает и просит разрешения на `git push`, вторая закончила ход и задаёт вопрос.
    Настоящего хоста сессий и `claude` нет: ленту пишем хостовым путём ядра."""
    for name, folder in _workdirs(Path(st.work_root)).items():
        r = await admin.patch(f"/admin/projects/{st.projects[name]}", json={"workdir": str(folder), "subs": True})
        r.raise_for_status()
    st.host_token = await _create_token(admin, name="work:host-demo", kind="background", scopes=["work_host", "read"])
    host = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {st.host_token}"})
    try:

        async def create(project: str, title: str, role: str) -> int:
            body = {"project_id": st.projects[project], "engine": "claude", "title": title, "role": role}
            return (await _post(public, "/api/v1/sessions", json=body, headers=headers))["session"]["id"]

        st.running_session = await create("Acme Storefront", "Cart badge shows 0 after login", "developer")
        await host_state(host, st.running_session, "running")
        await host_events(
            host,
            st.running_session,
            [
                {"kind": "start", "summary": "session started"},
                {"kind": "text", "summary": "Starting with the cart code and its tests."},
                {"kind": "tool", "tool": "Read", "summary": "src/cart/badge.py"},
                {"kind": "tool_done", "summary": "done (3120 chars)"},
                {"kind": "tool", "tool": "Bash", "summary": "uv run pytest -q tests/test_cart.py"},
                {"kind": "tool_done", "summary": "error (212 chars): FAILED test_badge_after_login", "is_error": True},
                {"kind": "turn_end", "summary": "turn finished: 4 steps, 52 s, 0 denials"},
                {"kind": "text", "summary": "Tests are green. The fix is committed; I want to push branch fix/badge."},
            ],
        )
        r = await host.post(
            f"/api/v1/work/host/sessions/{st.running_session}/requests",
            json={
                "kind": "permission",
                "tool": "Bash",
                "input": {"command": "git push origin fix/badge", "description": "Push the fix branch"},
                "reason": "hub guard: git push",
            },
        )
        r.raise_for_status()

        st.idle_session = await create("Globex Support", "Compare the renewal with the redline", "support")
        await host_state(host, st.idle_session, "idle")
        await host_events(
            host,
            st.idle_session,
            [
                {"kind": "start", "summary": "session started"},
                {"kind": "tool", "tool": "Read", "summary": "docs/renewal.md"},
                {"kind": "tool_done", "summary": "done (15200 chars)"},
                {"kind": "text", "summary": "The email says a 60-day notice, the redline still says 30."},
                {"kind": "turn_end", "summary": "turn finished: 3 steps, 31 s, 0 denials"},
            ],
        )
        question = {
            "question": "Which notice period should the reply cite?",
            "header": "Notice",
            "options": [
                {"label": "60 days (email)", "description": "what Tom agreed to in his message"},
                {"label": "30 days (redline)", "description": "what the document still says"},
            ],
            "multiSelect": False,
        }
        r = await host.post(
            f"/api/v1/work/host/sessions/{st.idle_session}/requests",
            json={"kind": "question", "tool": "AskUserQuestion", "input": {"questions": [question]}},
        )
        r.raise_for_status()
    finally:
        await host.aclose()
