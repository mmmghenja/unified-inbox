"""Ф88-1: руки агента владельца над сессиями хаба — ручки шаблонами openapi. Отдельный модуль без импортов
тестов: его берут и test_f88_agent_session_hands.py, и структурный тест test_f54_human_requests.py."""

# Исключения структурного теста «токен `human` — 403 везде»; что каждая открыта агенту — проверяет
# test_f88_agent_session_hands.py.
_S = "/api/v1/sessions/{session_id}"
AGENT_HANDS = frozenset(
    {
        ("post", "/api/v1/sessions"),
        ("get", "/api/v1/sessions"),
        ("get", _S),
        ("patch", _S),
        *(
            ("post", f"{_S}/{tail}")
            for tail in (
                "close",
                "handoff",
                "fallback/return",
                "compact",
                "wake",
                "mcp-reload",
                "say",
                "files",
                "says/{say_id}/resend",
                "interrupt",
                "sleep",
            )
        ),
        ("delete", f"{_S}/files/{{file_id}}"),
        ("delete", f"{_S}/says/{{say_id}}"),
        ("get", f"{_S}/events"),
        ("get", f"{_S}/events/{{event_id}}"),
        ("get", f"{_S}/role-text"),
        ("post", "/api/v1/work/pause/clear"),
        ("post", "/api/v1/subs/{sub_id}/end"),
        ("get", "/api/v1/subs"),
        ("post", "/api/v1/peer-msgs"),
    }
)

# Ф88-2: чтение переписки, поиск и сводка дня — агенту владельца (по запросу его модели).
AGENT_READS = frozenset(
    {
        ("get", "/api/v1/projects"),
        ("get", "/api/v1/projects/{project_id}/channels"),
        ("get", "/api/v1/projects/{project_id}/stages"),
        ("get", "/api/v1/projects/{project_id}/digest"),
        ("get", "/api/v1/channels/{channel_id}"),
        ("get", "/api/v1/channels/{channel_id}/messages"),
        ("get", "/api/v1/channels/{channel_id}/threads/{thread_ext_id}/messages"),
        ("get", "/api/v1/messages/{message_id}"),
        ("get", "/api/v1/search"),
    }
)
