# AGENTS.md

Rules for AI agents (and humans) working in this repository. See also [CLAUDE.md](CLAUDE.md).

## What this repository is

A public showcase of the `hub` multi-messenger (repository name: `unified-inbox`). It holds only invented data. The
UI and most code comments are in Russian; keep new comments in the language of the file you edit.

## Commands

```sh
uv sync                              # Python 3.14
uv run pytest -q -n 4                # tests; a single file: uv run pytest -q tests/<file>.py
uv run ruff check .                  # lint (the project does not enforce `ruff format`)
bash scripts/check-private.sh .      # must print nothing alarming: no real names, addresses, keys

cd ui && npm ci                      # Node 24
npm test && npm run lint
VITE_SHOWCASE=1 npm run build        # typecheck + production build

uv run python scripts/gen_mcp_docs.py   # regenerate docs/mcp.md after changing MCP tools
```

## Rules

- **Invented data only.** Use Acme, Globex, Initech, Umbrella, Hooli and `example.com` / `example.org` addresses. Never
  commit real names, e-mail addresses, phone numbers, tokens, session files or database files.
- **Secrets are never read, printed or committed.** The demo needs none.
- `docs/mcp.md` is generated from the MCP server; do not edit it by hand. `tests/test_mcp_docs.py` fails when it drifts.
- Screenshots in `docs/screenshots/` are PNG files without metadata chunks (no tEXt/eXIf/iTXt/tIME).
- Do not widen security boundaries (Host/Origin checks, CSRF, the permission guard, "send needs a human") to make a
  test pass. If a test and the boundary disagree, fix the test or ask.
- A new behavior needs a test that fails without the change. Run only the tests you touched, then the whole suite
  once before finishing.
- One migration number, one file: `src/hub/core/migrations/` takes the next free number; never reuse one.
- Keep changes focused: no drive-by renames or reformatting of unrelated lines.

## Layout

| Path | Purpose |
|---|---|
| `src/hub/core` | the core process: intake, routing, outbox, API, security |
| `src/hub/connectors` | connector skeleton, Telegram, mail, a fake source for tests and the demo |
| `src/hub/mcp_server` | MCP tools for agents |
| `src/hub/work`, `src/hub/engine` | agent session host and engine adapters |
| `ui/` | Vue 3 interface |
| `demo/` | the Docker demo: seed data and the launcher |
| `docs/` | architecture, MCP tool list, screenshots |
