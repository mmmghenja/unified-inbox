# unified-inbox (hub) — notes for Claude Code

Public showcase of a multi-messenger with an agent layer. The shared rules are in [AGENTS.md](AGENTS.md); read them
first. Architecture: [docs/architecture.md](docs/architecture.md).

- Python 3.14 and Node 24. Tests: `uv run pytest -q -n 4`, `cd ui && npm test`. Lint: `uv run ruff check .`,
  `cd ui && npm run lint`. Build: `cd ui && VITE_SHOWCASE=1 npm run build`.
- Real data never belongs here. Demo names: Acme, Globex, Initech, Umbrella, Hooli; addresses on `example.com`/`.org`.
  Run `bash scripts/check-private.sh .` before finishing.
- Data directories come from `HUB_DATA_DIR` / `HUB_CONFIG_DIR`; tests and demos always use temporary ones.
- Do not edit `docs/mcp.md` by hand: `uv run python scripts/gen_mcp_docs.py`.
- The core serves the built UI from `ui/dist`; there is no Vite dev server on purpose. Rebuild after UI changes.
- A change to a trust boundary (Host/Origin/CSRF checks, the unix-socket admin interface, the permission guard, the
  rule that a human confirms sending) needs a test and a note in `docs/architecture.md`.
- The Docker demo lives in `demo/`; try it with `docker compose up --build` and tear it down with `docker compose
  down -v`.
