"""SQLite link store: Plane work item <-> kanban task, plus reflection bookkeeping.

Single-writer (the sync process itself). Runs are serialized by a lock file,
so plain SQLite with a short transaction per row is enough.
"""

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    plane_issue_id     TEXT PRIMARY KEY,
    plane_project_id   TEXT NOT NULL,
    plane_project_name TEXT,
    kanban_task_id     TEXT,
    kanban_status      TEXT,
    reflected_status   TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_task ON links(kanban_task_id);
"""


class LinkStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, issue_id: str):
        cur = self.db.execute("SELECT * FROM links WHERE plane_issue_id = ?", (issue_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def all(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM links ORDER BY created_at")]

    def insert_link(self, issue_id, project_id, project_name, task_id, kanban_status=None):
        now = time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO links "
            "(plane_issue_id, plane_project_id, plane_project_name, kanban_task_id, "
            " kanban_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (issue_id, project_id, project_name, task_id, kanban_status, now, now),
        )
        self.db.commit()

    def update_status(self, issue_id, kanban_status):
        self.db.execute(
            "UPDATE links SET kanban_status = ?, updated_at = ? WHERE plane_issue_id = ?",
            (kanban_status, time.time(), issue_id),
        )
        self.db.commit()

    def mark_reflected(self, issue_id, status):
        self.db.execute(
            "UPDATE links SET reflected_status = ?, updated_at = ? WHERE plane_issue_id = ?",
            (status, time.time(), issue_id),
        )
        self.db.commit()
