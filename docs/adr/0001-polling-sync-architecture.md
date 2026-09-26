# ADR 0001: Polling sync with SQLite link store and CLI-mediated kanban writes

- Status: Accepted
- Date: 2026-09-26
- Decider: Maisarah (Engineering Manager), Phase 2 delivery (kanban t_99db4620)

## Context

Plane (self-hosted at `plane.alfirus.my`) is the source of truth for *what* we
are doing and *where* it stands; Hermes Kanban is the agent dispatch runtime
for *how* work executes. Phase 2 connects them with lightweight, bidirectional
plumbing — explicitly not a platform. Constraints that shaped this ADR:

- Windows host, git-bash; processes on this host have a demonstrated history of
  dying mid-life with no supervisor and no captured logs (see the 9119 outage
  post-mortem, card t_652f9a1e).
- No long-lived Python dependencies may be added (zero-install: stdlib only).
- Secrets must live in `~/.hermes-secrets/` (registry.yaml metadata), never in
  the repo, config, cards, comments or logs.

## Decision

1. **Transport: polling, not webhooks.** A one-shot sync pass runs every
   5 minutes from Windows Task Scheduler. Each pass reads mapped Plane
   projects, creates missing kanban tasks, and reflects terminal kanban
   states back to Plane. Webhooks were rejected: they require an inbound
   listener (extra always-on service, TLS/reachability, another thing to
   supervise) and Plane CE's outbound webhook configuration is state we would
   have to keep in sync anyway. Polling bounds both staleness (≤ interval) and
   failure surface.
2. **Runtime: scheduled one-shot task, not a daemon.** Because each pass is
   independent, "restart" is free — the next tick *is* the self-heal. A
   long-lived poller would inherit exactly the unattended-death failure mode
   this host has already hit three times in one day. A lock file with a
   30-minute stale-steal prevents overlapping passes.
3. **Link store: a small SQLite DB** (`~/.hermes/planesync/links.db`) keyed by
   Plane work item id, recording the kanban task id and the last reflected
   status. It is the idempotency anchor for both directions. SQLite over a JSON
   file for atomic single-row writes; over Postgres/Redis because the writer is
   a single serialized process and this is plumbing.
4. **Kanban writes go through the `hermes kanban` CLI** (JSON modes), not
   direct SQLite writes into `kanban.db`. The CLI is the supported interface —
   claim/idempotency semantics live behind it — so the sync does not depend on
   the board's internal schema. Belt-and-braces: task creation *also* passes
   `--idempotency-key plane:<issue-id>`, so even a lost link row cannot
   duplicate a task.
5. **One-way write rules (loop prevention):**
   - Plane → kanban: **create only**. Existing links are never re-written; the
     sync never edits a task it created. Items already in a `completed` /
     `cancelled` state group are not turned into tasks.
   - kanban → Plane: **reflect only**. `done` → move the linked work item to
     the project's `completed`-group state and post one summary comment;
     `blocked` → post one comment (no state change — `blocked` is runtime
     information that belongs to kanban). The sync never creates, edits or
     deletes Plane work-item text, assignees or priority.
   - Reflections are recorded per transition (`reflected_status`), so each
     transition is written to Plane at most once.
   - No loop is possible: kanban-side writes never create kanban tasks, and
     Plane-side writes never change anything that feeds task creation for a
     linked item.
6. **Mapping config is committed** (`config/mapping.json`, non-secret): Plane
   workspace, project → kanban board, assignee/priority maps. The API token is
   read at runtime from `~/.hermes-secrets/plane-sync.env`
   (`PLANE_API_TOKEN=`), never stored in the repo.
7. **Plane auth: personal access token** (`X-API-Key`, `/api/v1/…`) created
   for the `maisarah` staff account. Project membership is required per
   project — workspace role alone is not sufficient (verified: 403 with
   `is_member: false`).

## Alternatives considered

- **n8n / third-party connector**: already on the prod server, but adds an
  external platform dependency and credential surface for what is ~300 lines of
  plumbing; rejected as heavier than the job.
- **Direct writes to `kanban.db`**: shorter path, but bypasses board
  invariants and couples the sync to Hermes' internal schema; rejected.
- **Daemon poller with self-heal watchdog**: works, but re-creates the exact
  supervised-process problem this host keeps hitting; the scheduled one-shot
  has no process to supervise.
- **Webhook receiver (Plane → sync endpoint)**: lower latency, but needs an
  always-on listener and Plane webhook configuration; latency of ≤5 min is
  acceptable for a PM/dispatch hand-off.

## Consequences

- Sync latency is bounded by the polling interval (default 5 min); a manual
  `python -m planesync --once` gives immediate convergence.
- Failure of any single pass is visible in `~/.hermes/planesync/logs/planesync.log`
  (size-capped) and self-corrects on the next tick.
- Adding a Plane project = add one entry to `config/mapping.json` (plus the
  project must add the sync account as a member).
- Unmapped Plane assignees create **unassigned** kanban tasks (they sit in
  `todo` for lead routing) rather than being auto-dispatched to an arbitrary
  profile — deliberate: automation should not guess ownership.
