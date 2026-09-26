# hermeskanban-plane

Lightweight **bidirectional sync between Plane and Hermes Kanban** (Phase 2 of
the Plane/Hermes-Kanban tooling decision).

- **Plane** (`https://plane.alfirus.my`) = source of truth for *what* we are
  doing and *where* it stands (roadmap/issues, human-facing).
- **Hermes Kanban** (`~/.hermes/kanban.db`) = the agent dispatch runtime for
  *how* work executes.

This repo is the plumbing between them — deliberately small: one Python package,
stdlib only, no services to keep alive. Architecture decisions are recorded in
[docs/adr/0001-polling-sync-architecture.md](docs/adr/0001-polling-sync-architecture.md).

## How it works

A sync pass runs periodically (default: every 5 min via Windows Task Scheduler)
and does two things:

```
Plane (mapped projects)                     Hermes Kanban
  work item created/updated  --(1) create-->  task (idempotent, link stored)
  work item state            <--(2) reflect--  task done  -> state = Done + comment
  work item comments         <--(2) reflect--  task blocked -> comment only
```

### One-way write rules (loop prevention)

| Direction | May write | May NEVER write |
|---|---|---|
| Plane → kanban | create a task (title, body, assignee, priority) | edit/complete/archive an existing task |
| kanban → Plane | work-item **state** (on `done`) and **comments** (on `done`/`blocked`) | issue title, description, assignees, priority, creation/deletion |

- Task creation is idempotent twice over: a SQLite link store
  (`~/.hermes/planesync/links.db`) keyed by Plane work-item id, **and** the
  kanban CLI's own `--idempotency-key plane:<issue-id>`.
- Each kanban→Plane transition is reflected **at most once** (recorded in the
  link store), so re-syncs never double-post.
- Work items already in a `completed`/`cancelled` state group are never turned
  into tasks; items whose state cannot be resolved are deferred, not guessed.
- Unmapped Plane assignees produce an **unassigned** task (sits in `todo` for
  lead routing) — automation does not guess ownership.

## Setup

1. **Secrets** (never committed, never in cards/comments):
   `~/.hermes-secrets/plane-sync.env` must contain `PLANE_API_TOKEN=<token>`
   (Plane → Profile Settings → Personal Access Tokens). Metadata lives in
   `~/.hermes-secrets/registry.yaml` (`plane-sync-token` entry).
2. **Membership**: the sync account must be a **member of every mapped Plane
   project** — workspace role alone is not enough (API returns 403 with
   `is_member: false`).
3. **Mapping**: edit [`config/mapping.json`](config/mapping.json)
   (non-secret, committed). Keys under `projects` may be the Plane project
   *name*, *slug* or *id*; each entry can override `board` and
   `default_assignee`. `assignee_map` translates Plane assignees
   (display name or e-mail local part) to hermes profile names.
4. **Kanban CLI**: `hermes` must be on `PATH`.

## Run

```bash
# one pass (from the repo root)
PYTHONPATH=src python -m planesync --once

# options
PYTHONPATH=src python -m planesync --status       # show link store
PYTHONPATH=src python -m planesync --once --dry-run
PYTHONPATH=src python -m planesync --once --verbose
```

On Windows, `scripts\run_sync.cmd` wraps the same thing (sets `PYTHONPATH`).

**Important — clean environment:** the `hermes kanban` CLI refuses mutations
when `HERMES_DELEGATED_CHILD_CONTEXT` is set (it blocks agents from mutating
board state from inside a delegated/kanban-worker session). Run the sync from a
normal shell or the scheduled task — both are clean. If you must run it from
inside an agent session, unset the `HERMES_*` variables for that invocation.

### Scheduled task (production runtime)

```bat
scripts\install_scheduled_task.cmd     :: registers "HermesPlaneSync", every 5 min
scripts\uninstall_scheduled_task.cmd   :: removes it
```

Why a scheduled one-shot instead of a daemon: each pass is independent, so the
next tick *is* the self-heal — there is no long-lived process to supervise
(see ADR 0001 and the 9119 outage post-mortem for why that matters on this
host). A lock file prevents overlapping passes. Logs (size-capped):
`~/.hermes/planesync/logs/planesync.log`.

## Tests

```bash
python -m unittest discover -s tests -v
```

Covers: idempotent creation, done/blocked reflection happening exactly once,
assignee/priority mapping, state-id → group resolution, HTML→text body
conversion.

## End-to-end verification (2026-09-26)

See [docs/e2e-proof.md](docs/e2e-proof.md): a Plane issue in the
`hermeskanban plane` project became kanban task `t_432900bc`, was completed,
and the completion was reflected back (state → `Done`, one `[hermes-sync]`
comment with the run summary) — with API read-back proof.

## Layout

```
config/mapping.json      project/board/assignee mapping (non-secret)
src/planesync/           the sync package (stdlib only)
  config.py  plane.py  kanban.py  store.py  sync.py  __main__.py
scripts/run_sync.cmd     wrapper used by the scheduled task
scripts/install_scheduled_task.cmd
docs/adr/0001-*.md       architecture decision record
tests/test_sync.py       unit tests
```
