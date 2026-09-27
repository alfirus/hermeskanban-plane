"""Kanban side: talk to the shared board through the supported `hermes kanban` CLI.

The CLI is used deliberately instead of writing kanban.db directly: it is the
stable interface (atomic claim/idempotency semantics live behind it) and keeps
this sync from depending on the board's internal schema.

Invocation: with `cli: "auto"` (default) the command runs as
`python -m hermes_cli.main kanban ...` — the same CLI the gateway and the
kanban dispatcher use. The `hermes` bin launcher is NOT used by default: its
bootstrap (hermes_cli/venv_sync.prepare_launch) first finishes any interrupted
source update, and when that repair is wedged every launch blocks for minutes
(2026-09-27: PM worker died provisioning `npm` after an interrupted update;
scheduled `show` calls burned their whole 120s budget for ~2.5h). The module
entry runs the identical CLI without the repair step.

Troubleshooting: every CLI call is logged with its duration; a timeout error
carries the partial stdout/stderr the CLI managed to produce plus any known
platform-state hints (e.g. a pending source-update marker).
"""

import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("planesync")


class KanbanError(RuntimeError):
    pass


class KanbanTimeout(KanbanError):
    """The CLI did not exit within the budget; its process tree was killed."""


def _cli_venv_entry() -> str | None:
    """Locate the managed runtime's own `hermes` entry (e.g.
    <installs>/<key>/environments/<gen>/venv/Scripts/hermes.exe).

    That entry runs the CLI directly — unlike the `hermes` bin launcher, which
    first repairs interrupted source updates and can block for minutes. The
    selected environment is read from the install's facts.json."""
    try:
        roots = []
        if os.environ.get("HERMES_HOME"):
            roots.append(Path(os.environ["HERMES_HOME"]))
        if os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "hermes")
        roots.append(Path.home() / ".hermes")
        facts = []
        for root in roots:
            facts.extend(root.glob("installs/*/facts.json"))
        facts.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for fact in facts:
            env_dir = (json.loads(fact.read_text(encoding="utf-8"))
                       .get("packages", {}).get("venv", {}).get("environment"))
            if not env_dir:
                continue
            exe = (Path(env_dir) / "Scripts" / "hermes.exe" if os.name == "nt"
                   else Path(env_dir) / "bin" / "hermes")
            if exe.exists():
                return str(exe)
    except Exception:  # noqa: BLE001 - resolution must never break a pass
        return None
    return None


def resolve_cli_prefix(cli=None) -> tuple[list[str], str]:
    """Resolve the argv prefix that runs `hermes kanban ...`.

    - list/tuple: used verbatim (e.g. ["python", "-m", "hermes_cli.main"]).
    - a plain string: a single executable (legacy `"hermes"` launcher form).
    - None or "auto", in order: `[sys.executable, "-m", "hermes_cli.main"]`
      when hermes_cli is importable in this interpreter (the scheduled task's
      `python` often cannot); else the managed venv's own `hermes` entry; else
      the `hermes` launcher of last resort.

    Returns (argv_prefix, human-readable reason) for logging.
    """
    if isinstance(cli, (list, tuple)):
        return [str(c) for c in cli], "cli list from config"
    if isinstance(cli, str) and cli.strip() and cli.strip().lower() != "auto":
        return [cli.strip()], "cli executable from config"
    try:
        spec = importlib.util.find_spec("hermes_cli")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        return ([sys.executable, "-m", "hermes_cli.main"],
                "auto: hermes_cli importable in this interpreter")
    entry = _cli_venv_entry()
    if entry:
        return ([entry],
                "auto: hermes_cli not importable in this interpreter; using the "
                "managed venv CLI entry (direct, no launcher repair step)")
    return (["hermes"],
            "auto fallback: no hermes_cli import and no managed venv found; "
            "using the hermes launcher (its startup repairs interrupted source "
            "updates and can block)")


def platform_update_hints() -> str:
    """Best-effort stdlib-only diagnostics for known platform states that make
    `hermes` launcher starts block. Empty string when nothing is found."""
    try:
        roots = []
        if os.environ.get("HERMES_HOME"):
            roots.append(Path(os.environ["HERMES_HOME"]))
        if os.environ.get("LOCALAPPDATA"):
            roots.append(Path(os.environ["LOCALAPPDATA"]) / "hermes")
        roots.append(Path.home() / ".hermes")
        seen = set()
        for root in roots:
            if root in seen:
                continue
            seen.add(root)
            for marker in sorted(root.glob("installs/*/source-completion-pending")):
                try:
                    since = time.strftime("%Y-%m-%d %H:%M",
                                          time.localtime(marker.stat().st_mtime))
                except OSError:
                    since = "?"
                return (f"hermes source-update tail pending since {since} ({marker}); "
                        "the hermes launcher repairs it on every start and can block "
                        ">120s while the repair is broken — run `hermes update` to finish it")
            for name in (".update-incomplete", ".lazy-refresh-incomplete"):
                legacy = root / "hermes-agent" / name
                if legacy.exists():
                    return (f"hermes update marker present ({legacy}); "
                            "a platform update may be mid-flight")
    except Exception:  # noqa: BLE001 - diagnostics must never break a pass
        return ""
    return ""


def _parse_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise KanbanError(f"non-JSON output from hermes kanban: {text[:300]}")


class KanbanClient:
    def __init__(self, cli: str | list | None = None, board: str | None = None,
                 created_by: str = "planesync", timeout: int = 120):
        self.cli, why = resolve_cli_prefix(cli)
        self.board = board
        self.created_by = created_by
        self.timeout = timeout
        log.info("kanban CLI: %s (%s)", self.cli, why)

    def _run(self, args, stdin_text: str | None = None) -> str:
        cmd = list(self.cli) + ["kanban"]
        if self.board:
            cmd += ["--board", self.board]
        cmd += args
        label = " ".join(str(a) for a in args[:2])[:80]
        log.debug("kanban CLI spawn: %s", cmd)
        # Redirect stdout/stderr to temp files instead of pipes. The CLI spawns
        # helper children that inherit its handles; with pipes we would wait for
        # pipe EOF long after the CLI itself exited (observed live during a
        # Hermes self-update window, 2026-09-26), blowing past the timeout and
        # wedging the pass. A lingering grandchild holding a FILE handle never
        # blocks us: we wait for the direct child only, then read the files.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                    errors="replace") as out_f, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                       errors="replace") as err_f, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                       errors="replace") as in_f:
            if stdin_text:
                in_f.write(stdin_text)
                in_f.flush()
            in_f.seek(0)
            try:
                proc = subprocess.Popen(cmd, stdin=in_f, stdout=out_f, stderr=err_f)
            except (FileNotFoundError, OSError) as e:
                raise KanbanError(
                    f"kanban CLI not found: {' '.join(cmd[:2])} ({e})"
                ) from e
            t0 = time.monotonic()
            try:
                proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                # Kill the WHOLE tree: the wrapper alone can be gone while its
                # children keep the launch blocked, and we must fail fast so the
                # next scheduled tick retries.
                self._kill_tree(proc.pid)
                try:
                    proc.wait(timeout=10)  # reap; the tree is already dead
                except subprocess.TimeoutExpired:
                    pass
                out_f.seek(0)
                partial_out = out_f.read().strip()
                err_f.seek(0)
                partial_err = err_f.read().strip()
                detail = (f"kanban CLI timed out ({self.timeout}s): {label} "
                          f"[cmd: {' '.join(cmd)[:160]}; whole tree killed]")
                if partial_out:
                    detail += f" | partial stdout: ...{partial_out[-300:]}"
                if partial_err:
                    detail += f" | partial stderr: ...{partial_err[-300:]}"
                hint = platform_update_hints()
                if hint:
                    detail += f" | hint: {hint}"
                raise KanbanTimeout(detail) from None
            elapsed = time.monotonic() - t0
            out_f.seek(0)
            out = out_f.read()
            err_f.seek(0)
            err = err_f.read()
        if proc.returncode != 0:
            raise KanbanError(
                f"hermes {' '.join(args[:2])} failed ({proc.returncode}) "
                f"after {elapsed:.1f}s: {(err or out)[:400]}"
            )
        log.info("kanban CLI ok in %.2fs: %s (exit 0, %d B)", elapsed, label, len(out))
        return out

    @staticmethod
    def _kill_tree(pid: int):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=30)
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001 - best effort; the next pass retries
            pass

    def create_task(self, title: str, body: str, assignee: str | None = None,
                    priority: int | None = None, idempotency_key: str | None = None) -> dict:
        """Create a task; returns the task dict (idempotent when key given)."""
        args = ["create", title, "--body-file", "-", "--json",
                "--created-by", self.created_by]
        if assignee:
            args += ["--assignee", assignee]
        if priority is not None:
            args += ["--priority", str(priority)]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key]
        data = _parse_json(self._run(args, stdin_text=body))
        task = data.get("task", data)
        if not task.get("id"):
            raise KanbanError(f"create returned no task id: {json.dumps(data)[:300]}")
        return task

    def show(self, task_id: str) -> dict:
        data = _parse_json(self._run(["show", task_id, "--json"]))
        task = data.get("task", data)
        task["_runs"] = data.get("runs") or []
        return task
