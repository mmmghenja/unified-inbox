"""`docs/mcp.md` не расходится с MCP-сервером: те же инструменты и тот же текст, что даёт генератор."""

import importlib.util
from pathlib import Path

from hub.mcp_server.server import tool_names

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location("gen_mcp_docs", ROOT / "scripts" / "gen_mcp_docs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_lists_exactly_the_server_tools():
    doc = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    documented = _generator().documented_names(doc)
    assert sorted(documented) == sorted(tool_names("all"))
    assert len(documented) == len(set(documented))


def test_doc_is_up_to_date():
    gen = _generator()
    assert (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8") == gen.render(), (
        "docs/mcp.md устарел: uv run python scripts/gen_mcp_docs.py"
    )


def test_a_new_tool_would_break_the_check(monkeypatch):
    """Зубы теста: если сервер получил инструмент, которого нет в документе, сверка падает."""
    gen = _generator()
    doc = (ROOT / "docs" / "mcp.md").read_text(encoding="utf-8")
    first = gen.documented_names(doc)[0]
    stripped = doc.replace(f"## `{first}`", "## removed", 1)
    assert sorted(gen.documented_names(stripped)) != sorted(tool_names("all"))
