# End-to-end verification — 2026-09-26

Proves the Phase 2 Done criterion: *one Plane issue → kanban task → completion
reflected back in Plane, with read-back proof.* No secrets appear in this file.

## 1. Plane → kanban (creation)

Demo issue created in Plane project **hermeskanban plane** (workspace
`aerosgeotech`, project id `61ec245a-5177-44b8-9fb4-172d92e63956`):

```
POST /api/v1/workspaces/aerosgeotech/projects/61ec245a…/work-items/ -> 201
  id:         b39e68b8-c15c-45f3-9932-1981e48bdc33
  name:       [sync-demo] Phase 2 end-to-end verification
  sequence_id: 1   priority: high   state: Backlog
```

First sync pass:

```
planesync: created kanban task t_432900bc for Plane item b39e68b8… ([sync-demo] Phase 2 end-to-end verification)
summary: {"created": 1, "errors": 0, …}
```

Second sync pass (idempotency):

```
summary: {"created": 0, "skipped_existing": 1, "errors": 0, …}   # no duplicate
```

Link store read-back:

```
hermeskanban plane   issue=b39e68b8   task=t_432900bc   kanban=ready   reflected=-
```

Task content carried over (kanban read-back):

```
Task t_432900bc: [sync-demo] Phase 2 end-to-end verification
  status:    ready        assignee: -        created: 2026-09-26 18:01 by planesync

Body:
Synced from Plane (source of truth for scope/status).

- Plane project: hermeskanban plane (workspace aerosgeotech)
- Work item: [sync-demo] Phase 2 end-to-end verification (#1)
- State: Backlog
- URL: https://plane.alfirus.my/aerosgeotech/projects/61ec245a…/work-items/b39e68b8…
```

## 2. kanban → Plane (completion reflection)

Task completed:

```
hermes kanban complete t_432900bc --summary "Sync demo: task created from Plane
issue b39e68b8 by planesync; idempotent re-sync verified (no duplicate). …"
→ Completed t_432900bc   (status: done)
```

Sync pass:

```
planesync: reflected done to Plane item b39e68b8-c15c-45f3-9932-1981e48bdc33 (task t_432900bc)
summary: {"created": 0, "skipped_existing": 1, "reflected_done": 1, "errors": 0}
```

Re-run immediately after (no repeat writes):

```
summary: {"created": 0, "skipped_existing": 1, "reflected_done": 0, "errors": 0}
link store: hermeskanban plane  issue=b39e68b8  task=t_432900bc  kanban=done  reflected=done
```

## 3. Read-back from Plane (API, X-API-Key)

```
GET …/work-items/b39e68b8…/ -> 200
{
  "id": "b39e68b8-c15c-45f3-9932-1981e48bdc33",
  "name": "[sync-demo] Phase 2 end-to-end verification",
  "state_name": "Done", "state_group": "completed",
  "state_id": "a4253f11-51f3-4946-a735-45c7766b7a5d",
  "updated_at": "2026-09-26T10:03:29.258066Z"
}

GET …/work-items/b39e68b8…/comments/ -> 200
comment count: 1
  - by a5636b2c-dbda-41bf-9764-11a9bbceb46a (maisarah) at 2026-09-26T10:03:29Z:
    [hermes-sync] Kanban task t_432900bc went done. Agent summary: Sync demo:
    task created from Plane issue b39e68b8 by planesync; idempotent re-sync
    verified (no duplicate). Completing to prove reflection back to Plane.
    Reflected automatically by the Plane↔Hermes-Kanban sync.
```

Exactly one comment — the transition was reflected once, not on every poll.

## 4. Test suite

```
python -m unittest discover -s tests
Ran 10 tests in 0.550s — OK
```

Covers idempotent creation, done/blocked reflection exactly once,
assignee/priority mapping, bare-state-id resolution, HTML→text conversion,
and lock semantics (fresh lock blocks a second holder; stale lock is stolen).

## 5. Unattended round trip (Windows scheduled task)

Second demo issue, created in Plane first:

```
POST …/work-items/ -> 201   id 9190f5bf-c97b-4b27-a795-1ea7bd5cae89
  [sync-demo] scheduled-tick test   (#2, priority medium)
```

The scheduled task `HermesPlaneSync` (every 5 min, runs
`scripts\run_sync.cmd` from the durable clone `C:\Users\alfir\hermeskanban-plane`)
picked it up on its own tick — no shell involved:

```
2026-09-26 18:11:16  planesync: created kanban task t_25530120 for Plane item
                     9190f5bf… ([sync-demo] scheduled-tick test)
link store: issue=9190f5bf  task=t_25530120  kanban=ready  reflected=-
```

Task completed manually (`hermes kanban complete t_25530120 …`), then a later
pass reflected it:

```
2026-09-26 18:49:59  planesync: reflected done to Plane item 9190f5bf… (task t_25530120)
summary: {"created": 0, "skipped_existing": 2, "reflected_done": 1, "errors": 0}
```

Plane read-back (API):

```
GET …/work-items/9190f5bf…/ -> 200
  state_name: "Done",  state_group: "completed",  updated_at: 2026-09-26T10:50:00Z
GET …/comments/ -> 200   count: 1
  [hermes-sync] Kanban task t_25530120 went done. Agent summary: Scheduled-tick
  demo: created by the HermesPlaneSync Windows task (unattended), completing so
  the next tick proves unattended reflection. …
```

Final link store — both demo items round-tripped both ways:

```
hermeskanban plane  issue=b39e68b8  task=t_432900bc  kanban=done  reflected=done
hermeskanban plane  issue=9190f5bf  task=t_25530120  kanban=done  reflected=done
```

## 6. Live incident during verification (Hermes self-update window)

A Hermes self-update ran concurrently with verification (18:17–18:50) and made
`hermes kanban` launches intermittently block. Three defects surfaced and were
fixed (commits `e280cea`, `c04f460`, and the lock fix in this commit):

1. **Pipe-EOF wedge (critical).** `subprocess.run(timeout=…)` killed only the
   CLI wrapper on timeout while its children kept stdout/stderr pipes open, so
   `communicate()` waited forever — a pass wedged >10 min with no log progress
   (PID 6088, 18:13 tick). Fixed by killing the whole process tree.
2. **Temp files instead of pipes.** Even bounded, every tick's `show` blew past
   120s because the wait was on pipe EOF, not on the CLI itself. Redirecting
   stdout/stderr to temp files and waiting on the direct child took scheduled
   `show` from 120s-timeout to 3.3s (probe under the same scheduled context).
3. **Lock did not gate the body.** `Lock.__enter__` logged "skipping" but
   returned normally, so a second pass ran concurrently anyway. It now raises
   `LockHeld`, and `one_pass()` skips cleanly (rc 0), with stale-lock steal
   (>30 min) preserved.

Residual behavior while the platform updater is stuck: a tick's CLI launch may
still burn its 120s timeout, the pass logs the error, exits non-zero, and the
next tick retries — self-healing by design (ADR §failure handling); no data is
duplicated or double-reflected in the meantime (exactly-once tests above).

## 7. Live incident 2026-09-27: interrupted source update wedged `hermes` launches

Symptom: from ~12:15 to ~14:18 every scheduled `show` hit the 120s timeout
(`kanban show failed for t_…: kanban CLI timed out (120s)`), three per pass, so
each pass burned ~6.8 min and the following tick logged `another sync holds the
lock`. Plain `hermes kanban show` from an interactive shell returned in ~2.4s
throughout — the failure was specific to the scheduled task's process context.

Root cause (caught live with a 3s-interval process snapshot during a tick):

- `hermes kanban` was launched through the **`hermes` bin launcher**
  (`<HERMES_HOME>/bin/hermes.exe`), whose bootstrap
  (`hermes_cli/venv_sync.prepare_launch`) first finishes an **interrupted
  source update** (marker `installs/<key>/source-completion-pending`, present
  since 02:09 that night).
- The completion runs `source_completion.py --finish-update`, which needs
  `npm`; the PM worker died during provisioning
  (`pm.package.InstallError: pm: worker exited without a result`), so the
  completion failed and each launch paid 60–120s+ in retry/fallback before the
  actual command ran. Whether a given call squeaked under 120s was a race.
- The interactive shell never hit this because its `hermes` resolved to a
  managed **venv entry** (`environments/<gen>/venv/Scripts/hermes.exe`), which
  runs the CLI directly with no repair step. `python -m hermes_cli.main kanban
  …` (the gateway/dispatcher entry) likewise returned in ~2.9s.

Fixes (this repo):

1. **CLI invocation bypasses the launcher.** `kanban.cli: "auto"` (default)
   resolves in order: `python -m hermes_cli.main` when `hermes_cli` imports in
   the running interpreter; else the managed venv entry chosen via the
   install's `facts.json` (`packages.venv.environment`); else `hermes` as last
   resort. The scheduled task's `python` is 3.13 (Task Scheduler PATH), which
   cannot import `hermes_cli` — the venv-entry step is what saves it.
2. **Logging built for troubleshooting.** Startup line carries pid/interpreter;
   every CLI call logs its duration (`kanban CLI ok in 2.14s: show t_… (exit 0,
   N B)`); every pass logs a JSON summary with wall time; timeouts carry the
   partial stdout/stderr captured before the tree was killed plus a `hint:`
   when a known platform marker (e.g. `source-completion-pending`) is found.
3. **Circuit breaker.** The first CLI timeout aborts the rest of the pass
   (`"cli_wedged": true`), cutting worst-case pass time from ~6.8 min to one
   budget and keeping the next tick from piling up behind the lock.

Measured after the fix (same scheduled context, 14:48 tick): `show` 9.4s /
2.1s / 2.0s, pass 50.8s, zero errors — while the same hour's launcher-path
calls were still paying ~70s each.

Platform-side residue (outside this repo): the interrupted source update still
needs `hermes update` to finish (the `npm` PM failure is the thing to watch if
it recurs). planesync no longer touches that path, but any other automation
that spawns bare `hermes` still pays the penalty until it is cleared.

## Environment note

The `hermes kanban` CLI blocks mutations when `HERMES_DELEGATED_CHILD_CONTEXT`
is set (agent child sessions). The verification passes above ran with the
`HERMES_*` variables unset — the same environment the Windows scheduled task
provides (probe: `HERMES_HOME`, `HERMES_GIT_BASH_PATH` only). A probe task
created during this check was archived immediately (`t_c14f10b8`).
