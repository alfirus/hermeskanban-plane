"""Kanban side: talk to the shared board through the supported `hermes kanban` CLI.

The CLI is used deliberately instead of writing kanban.db directly: it is the
stable interface (atomic claim/idempotency semantics live behind it) and keeps
this sync from depending on the board's internal schema.
"""

import json
import os
import signal
import subprocess


class KanbanError(RuntimeError):
    pass


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
    def __init__(self, cli: str = "hermes", board: str | None = None,
                 created_by: str = "planesync", timeout: int = 120):
        self.cli = cli
        self.board = board
        self.created_by = created_by
        self.timeout = timeout

    def _run(self, args, stdin_text: str | None = None) -> str:
        cmd = [self.cli, "kanban"]
        if self.board:
            cmd += ["--board", self.board]
        cmd += args
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as e:
            raise KanbanError(f"kanban CLI not found: {self.cli}") from e
        try:
            out, err = proc.communicate(input=stdin_text, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # Kill the WHOLE tree: the CLI wrapper spawns children that inherit
            # the pipes, and a plain kill of the wrapper leaves communicate()
            # blocked on pipe EOF forever (observed live during a Hermes
            # self-update window, 2026-09-26).
            self._kill_tree(proc.pid)
            try:
                proc.communicate(timeout=15)
            except Exception:  # noqa: BLE001
                pass
            raise KanbanError(
                f"kanban CLI timed out ({self.timeout}s): {' '.join(args[:2])}"
            ) from None
        if proc.returncode != 0:
            raise KanbanError(
                f"hermes {' '.join(args[:2])} failed ({proc.returncode}): "
                f"{(err or out)[:400]}"
            )
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
