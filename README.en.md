# unified-inbox

**English** · [Русский](README.md)

A personal multi-messenger for working by project. Telegram and e-mail (IMAP/SMTP, Gmail) flow into one feed laid out
by project, and you answer from the same window. AI agent sessions (Claude Code, OpenCode, Codex, Cursor) attach to
the same feed: they read it through an MCP server, put reply drafts in, and decide what to do with incoming items,
while the decision to send stays with the human.

This is the public showcase of a large personal project: the working name in the code is `hub`, the repository is
`unified-inbox`. There is not a single real message, address or secret here, and the demo runs on invented data.

![Home: the project board](docs/screenshots/board-1440.png)

| Conversation | "Waiting for you" | Phone | Sessions, phone |
|---|---|---|---|
| ![Conversation](docs/screenshots/conversation-1440.png) | ![Waiting](docs/screenshots/waiting-1440.png) | ![Waiting, 390 px](docs/screenshots/waiting-390.png) | ![Sessions, 390 px](docs/screenshots/sessions-390.png) |

## What is inside

- **Core** (`src/hub/core`): one FastAPI + SQLite process. Event intake, routing into projects, an outbound queue
  (`outbox`) with retries and an "unclear outcome" state, an SSE stream for the UI, the security rules.
- **Connectors** (`src/hub/connectors`): one process per account, talking to the core over an internal socket. A
  connector brings events in and takes outbound items out; it decides nothing for the core.
- **MCP server** (`src/hub/mcp_server`): 60 tools for agents: reading, drafts, subscriptions, permission cards.
  List: [docs/mcp.md](docs/mcp.md).
- **Agent sessions** (`src/hub/work`, `src/hub/engine`): the host starts Claude Code / OpenCode / Codex / Cursor
  sessions and keeps their conversation, the permission guard and the run state.
- **UI** (`ui/`): Vue 3 + Vite + TypeScript, no router and no store library; served by the core itself.

The design in detail: [docs/architecture.md](docs/architecture.md). How it was built with AI coding agents — the
process, roles and the rules it left behind: [docs/how-it-was-built.md](docs/how-it-was-built.md).

## Quick start: the demo in Docker

You need Docker and Docker Compose.

```sh
docker compose up --build
docker compose logs hub          # the log prints a sign-in URL with a one-time code
```

Open the printed address (`http://localhost:8787/...`). The code lives for 60 seconds; to get a new URL:

```sh
docker compose exec hub python -m demo.serve code
```

For another host port use `HUB_DEMO_PORT=9000 docker compose up`. The demo listens on the host loopback only, runs
in the container as an unprivileged user (read-only file system, no capabilities) and seeds itself with invented
projects (Acme, Globex, Initech), Telegram and mail conversations, two agent sessions and permission cards. A new
incoming message arrives about every half minute. To stop and wipe the data: `docker compose down -v`.

## Development

```sh
uv sync                      # Python 3.14
uv run pytest -q -n 4        # about 8,600 tests
uv run ruff check .

cd ui
npm ci                       # Node 24
npm test                     # about 2,200 tests
VITE_SHOWCASE=1 npm run build
```

Entry points for reading the code:

| What | Where |
|---|---|
| core start-up, the two servers, life cycle | `src/hub/core/runner.py` |
| event intake, de-duplication, routing | `src/hub/core/ingest.py` |
| outbound queue and the unclear outcome | `src/hub/core/outbox.py`, `src/hub/connectors/base.py` |
| MCP tools and trust boundaries | `src/hub/mcp_server/server.py` |
| UI | `ui/src/App.vue`, `ui/src/store/store.ts` |

## Limitations of this version

- The UI and the code comments are in Russian, and so are the names and descriptions of MCP tools.
- This is a slice of a working system: the public version has only the Telegram and mail connectors. The other
  bridges (Slack, Mattermost, WhatsApp) live in the private version and are not included; the connector skeleton
  (`connectors/base.py`) is shared.
- The demo is a showcase, not a product to install: it does not connect to real messengers.
- The deploy script and the systemd units (`scripts/hub-deploy.sh`, `deploy/systemd/`) assume the layout
  `~/projects/hub-prod` (a separate worktree on the `prod` branch) — adjust the paths for your setup.
- `docs/mcp.md` is generated from the full MCP server, so it still mentions tools and wording of the bridges that
  are not included (WhatsApp group tools, Slack and Mattermost threads).

License: [MIT](LICENSE). Third-party packages: [THIRD_PARTY.md](THIRD_PARTY.md). Rules for agents and contributors:
[AGENTS.md](AGENTS.md).
