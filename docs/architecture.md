# Architecture

This is the map of the codebase. Source comments and docstrings (in Russian) cite this file; the
[section map](#section-names-cited-in-the-source) at the bottom says where each cited name lives.

The product: one inbox for many messengers and mailboxes, sorted into projects, with replies sent from the same
window. Coding-agent sessions (Claude Code, OpenCode, Codex, Cursor) work next to the inbox and talk to it over
MCP. The owner of the hub is the only human; agents are clients of the same API.

```
                 +-----------------+     unix socket (internal API)     +---------------------+
 Telegram  ----> | connector       | <--------------------------------> |                     |
 Mail      ----> | processes       |                                    |      hub core       |
 (fake)    ----> | (one per        |                                    |  one writer, SQLite |
                 |  account)       |                                    |                     |
                 +-----------------+                                    |  public API :8787   | <--- browser (Vue UI, SSE)
                                                                        |  /api/v1 + UI       | <--- MCP server (stdio) <--- agent sessions
                 +-----------------+     bearer token, same API         |                     |
                 | session host    | <--------------------------------> |                     |
                 | (hub work)      |                                    +---------------------+
                 +-----------------+
```

## Process model: `hub core`

`src/hub/core/runner.py`. One process, one event loop, **two** ASGI applications served by two `uvicorn` servers:

| Application | Listens on | Clients | Auth |
|---|---|---|---|
| public (`app_public.py`) | `127.0.0.1:8787` | browser UI, MCP servers, agents, session host | cookie session + CSRF (UI), bearer tokens with scopes (everything else) |
| internal (`app_internal.py`) | unix socket `<data_dir>/internal.sock` (directory 0700, socket 0600) | connectors, the CLI | per-account connector secret, admin key |

An optional third listener (`remote.py`, `tls.py`) serves the UI to a phone through an SSH/VPN tunnel: it accepts
only password login, refuses any `Authorization` header and can speak TLS. Everything shares one `Db`, one
`Notifier` (wakes long-polls and SSE) and one clock; the clock is always passed in as `now_ms`, business code
never calls `time.time()`.

A second instance is prevented twice: an `flock` on `core.lock` and a liveness probe of the socket.

Other long-running processes are small and separate: connectors (`hub-connector@<account>`), the session host
(`hub-work`), the rule-based triage agent (`hub-agent`), the watchdog (`hub-watch`, deliberately **not** routed
through core so it notices core dying), the encrypted backup (`hub-backup`), notifications, voice transcription.
Units are in `deploy/systemd/`.

## Data: `src/hub/core/db.py`, `migrations/`

SQLite in WAL mode. **One writer thread per process** with a queue, read-only connections for readers. All writes go
through `Db.write()`, which opens `BEGIN IMMEDIATE`; the domain modules (`ingest.py`, `outbox.py`, ...) are plain
functions that run inside that transaction and receive `now_ms` as a parameter, which makes them testable without
mocking time.

Two database files, attached to one connection: `hub.db` (main) and `hub-work.db` (work sources, with their own
retention and "delete the file to delete the data"). Triggers and FTS5 external-content tables cannot cross a file
boundary, so each file has its own copy of the shared tables; references that cross it (`account_id`, `project_id`,
`api_token_id`) carry no `FOREIGN KEY` and are checked in code. Row ids from the work file start at `2**40`.

Core tables: `project`, `account`, `channel` (`dm | group | channel | topic | mail_thread | ...`), `message`
(unique per `(channel_id, ext_id)`, a global monotonically growing `seq`), `outbox`, `map_rule`, `api_token`,
`audit` (append-only, ids and statuses only, never message text), `dead_letter`. Unread is not a counter: it is
computed from `seq > last_read_seq AND NOT is_me`. Search is FTS5 over `message`.

Migrations are numbered SQL files; **one number is exactly one file**, and the version is `PRAGMA user_version`.

## Inbound: connectors and `ingest.py`

A connector is a process that owns one account of one source. It subclasses `ConnectorBase`
(`src/hub/connectors/base.py`) and implements four methods:

```python
async def sync(self, reason): ...      # pull new events of the source, hand them to emit()
async def send(self, lease): ...       # deliver one outbound message; never called for reconcile leases
async def reconcile(self, lease): ...  # did it go out? answers only "sent" or "unknown"
async def health(self): ...            # is the source reachable and the login valid
```

`ConnectorBase.run()` is the outer loop: buffering, back-off with jitter while core is unreachable, resync on every
reconnect and after sleep, a bounded event buffer with catch-up from the cursor instead of silent loss. Connectors
talk to core through `CoreClient`, an HTTP client over the unix socket, authenticated by the account's secret; a
connector can only write to its own account.

Events are typed (`upsert | edit | delete | reaction | read | ...`, `core/events.py`) and pushed in batches of up to
500 together with the new cursor value, so events and cursor are committed in **one transaction**. `ingest.apply_batch`
applies each event inside its own `SAVEPOINT`: a failing event is rolled back into `dead_letter` while its neighbours
apply, and the cursor still moves. Everything the UI, filters and routing need (mentions, forwards, albums, topics,
buttons, attachments) is extracted at ingest time into dedicated columns; raw payloads are not the source of truth.

Shipped connectors: **Telegram** (`connectors/telegram`, a personal account over MTProto with Telethon), **mail**
(`connectors/mail`, IMAP/SMTP and Gmail, threads, notification mail from trackers folded into one conversation per
task) and **fake** (`connectors/fake.py`, an in-memory source with a scripted `FaultPlan`; it drives the end-to-end
tests and the Docker demo). The interface above is all a new source needs.

Routing to projects is deterministic: manual assignment, then `map_rule` by priority, then "Unsorted". The reason is
stored next to the channel.

## Outbound: `outbox.py`

The state machine that makes "send" safe against crashes, sleep and retries.

1. `POST /messages/send` creates an `outbox` row and the placeholder `message` row in **one transaction**, so the
   conversation shows exactly one line from the first second.
2. A connector takes work with a lease (`UPDATE ... SET status='sending', lease_owner, lease_expires_ts WHERE
   status='queued' AND not_before_ts <= now AND the account is not in flood wait`). Time filtering lives in core.
3. The connector sends with a `send_token` so the source can deduplicate (a deterministic `random_id` in Telegram,
   a controlled `Message-ID` in mail).
4. The result moves the row to `sent` with the source id. The echo from the source is merged into the same row by
   `send_token` or `Message-ID`, or by `is_me` + channel + text inside a time window. Echo before result is harmless.
5. A lease that expires without a result is retried blindly only where the source guarantees idempotency;
   elsewhere core asks the connector to `reconcile`, and if the answer is not unambiguous the status becomes
   **`unknown`** ("maybe sent, check"). A human decides. Nothing is ever retried blindly.

Statuses: `pending_approval | queued | sending | sent | failed | unknown | cancelled`. Flood waits are stored in the
database and survive restarts.

**The hub does not send on its own.** Anything an agent writes is a draft (`pending_approval`); the owner confirms it in the
UI. Two narrow, owner-controlled exceptions exist: conversations the owner marks "send without confirmation" for a
named role (`autosend.py`, with rate limits), and autopilot switches that the owner clears one by one in the UI
(`autopilot.py`). Neither can be widened from code or by an agent.

## Agent access: MCP (`src/hub/mcp_server`)

`hub mcp --profile NAME` is an MCP server on stdio. A profile is a bearer token with scopes and an optional project
scope, stored in `<config_dir>/mcp/` (mode 0600). It exposes the inbox to a coding agent: read, search, draft, organize,
subscribe, send files, remind, hand work to another session. The full list of tools, with descriptions and parameters,
is generated from the server itself: [mcp.md](mcp.md).

Trust model for tool output: message texts, conversation titles and sender names are written by strangers. Every tool
result wraps them in a data block with a fresh random nonce (`----- ДАННЫЕ <nonce> ... ----- КОНЕЦ ДАННЫХ <nonce> -----`: "DATA" / "END OF DATA"), and
the server instructions tell the model that nothing inside a block is an instruction. Secrets are masked before text
leaves core.

Long waits are cheap: a session subscribes to a project or a conversation and sleeps on `hub sub wait`, which
returns only counts ("3 new, 1 draft decision") when something happens; the session then reads what it needs.

## Sessions and engines: `src/hub/work`, `src/hub/engine`

A **session** is a long-lived coding-agent process attached to a project (its working folder). `hub work run` is the
host: it starts and resumes sessions, relays events (steps, tool calls, streamed text, cost) to core, delivers the
owner's messages, handles usage limits and pauses.

An **engine** is the CLI the session runs on. `engine/registry.py` holds one profile per engine (Claude Code,
OpenCode, Codex, Cursor): binaries, effort and model choices, MCP setup, hooks. The same rules and the same MCP
tools apply to all of them; the difference is the model.

Every tool call goes through a **guard** hook (`work/guard.py`, `hub-work-guard`): it decides allow, deny (the model
gets the reason and continues) or ask, which becomes a **card** in the owner's "Waiting for you" list. The set of
rules the owner has lifted (autopilot) is asked from the host over an abstract unix socket on every call and checked
by `SO_PEERCRED`; it is deliberately not a file the session could rewrite.

Sessions of one project are equal peers with a role each. They can message each other through core
(`peer_msgs.py`), subscribe to conversations with keyword filters, and ask the owner a question that waits in the
card list. The owner talks to any of them from the UI.

## UI: `ui/`

Vue 3 + TypeScript + Vite, no router, no state library: `ui/src/store/store.ts` is a plain reactive store.
Live updates arrive over SSE (`GET /stream`); `GET /sync?since=<seq>` returns every change after a sequence number
and is used for catch-up and as a periodic safety poll, so a missed frame never leaves the screen stale. The built bundle (`ui/dist`) is served by core itself. Layouts: board of
columns on a wide screen, tabs by project, a phone layout with a bottom bar. A build flag `VITE_SHOWCASE=1` hides
the owner's private agent features in this edition.

Tests: Vitest with jsdom (`npm test`), type checks with `vue-tsc` (`npm run typecheck`).

## Security model

The honest threat model: the main adversary is not another user but any process running under the same uid and any
browser tab.

- **Browser tabs:** the public listener checks `Host` and `Origin` against exact values, rejects `OPTIONS`, requires
  a CSRF header on every unsafe method, and sets strict security headers. A UI session is a cookie
  (`SameSite=Strict`) obtained with a one-time login code (256 bit, stored as a hash, 60 s TTL) printed by `hub ui`.
- **Other processes:** data and config directories are 0700, secrets are files with mode 0600. This is a soft
  boundary, acknowledged as such. Disk encryption is the owner's job.
- **Bearer tokens:** hashed in the database, scoped (`read`, `draft`, `organize`, `attach`, ...) and optionally limited to
  projects; some scopes can never be combined. Bearer tokens cannot reach the remote (phone) listener at all.
- **Secrets:** connectors keep their credentials in 0600 files, entered by the owner with `hub auth <source>`; they
  never appear in the database config, logs or the repository.
- **Logs and audit:** no message texts. The audit table records ids and statuses.
- **Backups:** `VACUUM INTO` of a read-only connection, encrypted with `age` to a public recipient; the private key
  is never on the machine. Without a valid recipient no backup is made.
- **Watchdog:** `hub watch` runs outside core and raises a desktop notification when core is down, an account needs
  login, the dead-letter queue grows or a backup is stale.

## Testing

`uv run pytest -q` (asyncio mode, `pytest-xdist`), roughly 8,600 tests: unit tests for the pure state machines, API
tests against the real ASGI app, connector tests against recorded fixtures, and end-to-end scenarios over the
`FakeConnector` with scripted faults (crash before send, flood wait, unknown outcome, sleep/resume). Time is injected,
so no test sleeps for real. `npm test` in `ui/` runs about 2,200 component and store tests.

## Section names cited in the source

Source comments (in Russian) refer to sections of this document by name.

| Cited as | See |
|---|---|
| «Процесс hub core», 1.x «Архитектура» | [Process model](#process-model-hub-core) |
| 1.4 «БД», «Слой БД» | [Data](#data-srchubcoredbpy-migrations) |
| 1.1 «Поток входящих», «Приём событий» | [Inbound](#inbound-connectors-and-ingestpy) |
| 1.2 «Поток исходящих», «Автомат исходящих» | [Outbound](#outbound-outboxpy) |
| «Клиент коннектора», «Fake-коннектор», «Стенд» | [Inbound](#inbound-connectors-and-ingestpy) |
| «Вход UI», «Bearer», «Principal и скоуп», 7 «Эксплуатация и безопасность» | [Security model](#security-model) |
| 7.2 «Бэкап», 7.3 «hub watch» | [Security model](#security-model) |
| «Этап 4b», `/sync`, `/stream` | [UI](#ui-ui) |
| 3.x «Сессии», 6.x «Движки» | [Sessions and engines](#sessions-and-engines-srchubwork-srchubengine) |
| any numbered phase or "spec" reference | design history of the private edition; the code comment describes the behaviour |
