"""Kanban side: talk to the shared board through the supported `hermes kanban` CLI.

The CLI is used deliberately instead of writing kanban.db directly: it is the
stable interface (atomic claim/idempotency semantics live behind it) and keeps
this sync from depending on the board's internal schema.
"""

import json
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
            proc = subprocess.run(
                cmd, input=stdin_text, capture_output=True, text=True,
                timeout=self.timeout, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as e:
            raise KanbanError(f"kanban CLI not found: {self.cli}") from e
        except subprocess.TimeoutExpired as e:
            raise KanbanError(f"kanban CLI timed out: {' '.join(args[:2])}") from e
        if proc.returncode != 0:
            raise KanbanError(
                f"hermes {' '.join(args[:2])} failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout)[:400]}"
            )
        return proc.stdout

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
