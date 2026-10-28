# How this was built

This repository is a public cut of a system I use every day. I built it in about six weeks, starting in
September 2026, and almost all of the code was written by AI coding agents working under a process I designed
and ran. This page describes that process, because it is as much the point of the project as the code.

## The numbers

- About 40 working days from the first commit to this snapshot.
- Roughly 2,250 commits in the private repository and about 200 written specifications, split into close to
  100 phases.
- About 8,600 Python tests and 2,200 UI tests in this public cut; the full private version has more.
- Four agent runtimes supported by the session host: Claude Code, OpenCode, Codex and Cursor.

## Roles

The work is split between agents with different jobs and, deliberately, different models.

| Role | What it does | Model |
|---|---|---|
| Architect (one long-lived session) | Owns the plan, writes the spec for each piece, accepts or rejects the result, keeps the trust boundaries | Strongest available model |
| Implementer | Writes one piece of code from a spec, with its tests | Mid-tier model for ordinary pieces; strongest model for the core, the outbox, migrations and security boundaries |
| Plan critic | Attacks a spec before any code exists: wrong assumptions, missed consumers, unsafe defaults | Strongest model, one round only |
| Code reviewer | Reviews the finished branch in a fresh context and must reproduce every finding with a failing test | Strongest model, one review per branch |
| Explorer | Answers "where is this, who calls it, what will this change touch" without dumping files into the architect's context | Mid-tier model, read-only |

Two rules came out of experience. The reviewer must not be the model that wrote the code. And critics never run
inside an implementer: one session orchestrates, every other agent does one job and returns a short report.

## One piece, end to end

1. **Spec.** The architect writes a spec: the behaviour, the files and functions involved, the data contract, the
   failure cases, the commands that prove it works. Risky pieces (sending messages to people, data loss, schema
   migrations, security) go to the plan critic first. Its blockers are checked against the code and folded into the
   spec as numbered amendments that take priority over the original text.
2. **Implementation.** An implementer works in its own git worktree, so parallel pieces never touch each other.
   It reports a list of changed files and how to verify them, nothing more.
3. **Acceptance.** The architect does not trust the report. It reads the diff of the risky places, runs the tests
   that matter, and for user-visible work drives the real UI in a headless browser at desktop and phone widths.
4. **Review.** Responsible branches get one independent code review with reproduced findings. Ordinary pieces are
   accepted on tests and the diff alone. A second review round is not allowed; fixes after review are proven by
   tests.
5. **Live stand.** Anything that talks to the outside world is run against a real stand before it ships: a local
   Mattermost server in Docker, a real Claude Code or OpenCode process, a fake connector that injects failures.
6. **Deploy.** Production runs from a separate worktree on its own branch. The deploy script backs up the data,
   fast-forwards the branch, rebuilds the UI and restarts the services. The agents prepare it; the owner runs it.

Each spec ends with an acceptance section: who wrote the piece, how many defects were found on acceptance, and
what was knowingly left as debt. That record is what let the process be tuned. For example, which classes of work
a cheaper model can be trusted with was decided from those numbers, not from guesses.

## Things that went wrong and the rule each one left behind

- **Two migrations with the same number** shipped in two deploys; the second one was silently skipped because the
  database stores a single version number. Rule, enforced by a test: one number, one migration file.
- **Tests that only passed on my machine.** The first CI runs of this public repository failed on things the
  developer machine had always provided: a Russian locale and Moscow time zone, a 16 GB `/dev/shm` for test data
  (the runner has 64 MB, so a free-space guard refused uploads), more CPU cores than a two-core runner, and a
  production checkout at a fixed path. The tests now adapt to the machine, and the export script reads the
  environment and the exact commands from the CI workflow and runs them before anything is pushed.
- **A full test run that froze the laptop**: SQLite databases in temporary directories wrote tens of gigabytes to
  disk. Temporary test data now lives in memory, heavy runs go through a small load registry with a memory
  ceiling, and the full suite runs once at the end of a phase instead of after every piece.
- **Review findings that nobody could reproduce** cost time to argue about. Now every finding comes with a
  failing test, or it is not a finding.

## Guarding agents that act on real data

The hub sends messages to real people, so the agent layer is built around a few hard rules:

- An agent never sends on its own by default. It leaves a draft; a person confirms it in the UI. The exceptions are
  narrow, explicit and switched on per conversation or per project by the owner.
- Sessions run behind a permission guard that turns pushes, deploys, shell access to other machines and writes
  outside the project folder into cards a person approves.
- Everything an agent reads from a chat is wrapped as data with a per-call marker, so a message that says "ignore
  your instructions" stays a quoted message.
- Secrets are entered by the owner through an interactive command and never pass through an agent.

## What is not in this repository

The private version also has connectors for Slack, Mattermost and WhatsApp, a personal agent that answers
approval cards on the owner's behalf, and a few personal services. They were cut from this public snapshot by
an export script with an allow-list, cut markers in the source, replacement rules and two leak checkers. One of
the checkers ships here as `scripts/check-private.sh`.
