"""Unit tests for planesync: idempotent creation + one-way reflection rules.

Run from the repo root:
    python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planesync.store import LinkStore          # noqa: E402
from planesync.sync import (                   # noqa: E402
    assignee_for, html_to_text, run_sync,
)


def make_config(tmp: str) -> dict:
    return {
        "plane": {"base_url": "https://plane.example", "workspace": "ws"},
        "kanban": {"cli": "hermes", "board": None, "created_by": "planesync"},
        "assignee_map": {"alice@corp": "alice-profile"},
        "priority_map": {"high": 70, "medium": 50, "none": 10},
        "default_assignee": None,
        "skip_state_groups": ["completed", "cancelled"],
        "projects": {"demo": {}},
        "data_dir": tmp,
    }


def item(iid, name, group="unstarted", assignees=(), priority="medium"):
    return {
        "id": iid, "name": name, "sequence_id": 1, "priority": priority,
        "state": {"id": "s-" + group, "name": group, "group": group},
        "assignees": [{"display_name": a} for a in assignees],
        "description_html": "<p>hello <b>world</b></p>",
    }


class FakePlane:
    def __init__(self, items, identifier=None):
        self.items = items
        self.identifier = identifier
        self.state_changes = []
        self.comments = []

    def list_projects(self):
        p = {"name": "demo", "id": "p1"}
        if self.identifier:
            p["identifier"] = self.identifier
        return [p]

    def list_work_items(self, project_id):
        return list(self.items)

    def list_states(self, project_id):
        return [
            {"id": "s-backlog", "name": "Backlog", "group": "backlog"},
            {"id": "s-unstarted", "name": "Todo", "group": "unstarted"},
            {"id": "s-started", "name": "In Progress", "group": "started"},
            {"id": "s-completed", "name": "Done", "group": "completed"},
            {"id": "s-cancelled", "name": "Cancelled", "group": "cancelled"},
        ]

    def completed_state_id(self, project_id, cache):
        return "s-completed"

    def set_work_item_state(self, project_id, item_id, state_id):
        self.state_changes.append((item_id, state_id))

    def add_comment(self, project_id, item_id, comment_html, external_id=None,
                    external_source=None):
        self.comments.append((item_id, comment_html, external_id))


class FakeKanban:
    def __init__(self):
        self.created = []
        self.tasks = {}
        self._n = 0

    def create_task(self, title, body, assignee=None, priority=None,
                    idempotency_key=None):
        if idempotency_key:
            for t in self.tasks.values():
                if t.get("idem") == idempotency_key:
                    return t  # kanban's own dedup kicks in
        self._n += 1
        task = {"id": f"t_{self._n}", "title": title, "body": body,
                "assignee": assignee, "priority": priority, "status": "todo",
                "idem": idempotency_key}
        self.tasks[task["id"]] = task
        self.created.append(task)
        return task

    def show(self, task_id):
        t = self.tasks[task_id]
        return {**t, "_runs": [{"summary": t.get("summary")}]}
        return t


class SyncFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LinkStore(Path(self.tmp.name) / "links.db")
        self.cfg = make_config(self.tmp.name)
        self.plane = FakePlane([item("i1", "Build widget"),
                                item("i2", "Old work", group="completed"),
                                item("i3", "Review", assignees=("alice@corp",),
                                     priority="high")])
        self.kanban = FakeKanban()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_pass(self):
        return run_sync(self.cfg, self.plane, self.kanban, self.store)

    def test_creation_is_idempotent(self):
        s1 = self.run_pass()
        self.assertEqual(s1["created"], 2)          # i2 is completed -> skipped
        self.assertEqual(s1["skipped_terminal"], 1)
        s2 = self.run_pass()
        self.assertEqual(s2["created"], 0)
        self.assertEqual(s2["skipped_existing"], 2)
        self.assertEqual(len(self.kanban.created), 2)

    def test_string_state_resolved_via_state_map(self):
        # live API returns state as a bare id, not an object
        self.plane.items = [{"id": "i9", "name": "Finished elsewhere", "sequence_id": 9,
                             "priority": "low", "state": "s-completed",
                             "assignees": [], "description_html": ""}]
        s = self.run_pass()
        self.assertEqual(s["skipped_terminal"], 1)
        self.assertEqual(s["created"], 0)
        self.plane.items = [{"id": "i8", "name": "Open item", "sequence_id": 8,
                             "priority": "low", "state": "s-unstarted",
                             "assignees": [], "description_html": ""}]
        s = self.run_pass()
        self.assertEqual(s["created"], 1)

    def test_project_identity_injected(self):
        # staff-suggestions v1.6 Appendix A: [IDENT] title prefix + Project: header
        self.plane.identifier = "DEMO"
        self.run_pass()
        t = self.kanban.created[0]
        self.assertEqual(t["title"], "[DEMO] Build widget")
        self.assertTrue(t["body"].startswith("Project: demo (DEMO)\n"))
        self.assertIn("Project: demo (DEMO)", t["body"].splitlines()[0])

    def test_project_identity_falls_back_without_identifier(self):
        self.run_pass()   # FakePlane without identifier
        t = self.kanban.created[0]
        self.assertEqual(t["title"], "Build widget")
        self.assertTrue(t["body"].startswith("Project: demo\n"))

    def test_assignee_and_priority_mapping(self):
        self.run_pass()
        by_title = {t["title"]: t for t in self.kanban.created}
        self.assertIsNone(by_title["Build widget"]["assignee"])   # unmapped -> unassigned
        self.assertEqual(by_title["Review"]["assignee"], "alice-profile")
        self.assertEqual(by_title["Review"]["priority"], 70)

    def test_done_reflects_state_and_comment_exactly_once(self):
        self.run_pass()
        tid = self.kanban.created[0]["id"]           # task for i1
        self.kanban.tasks[tid]["status"] = "done"
        self.kanban.tasks[tid]["summary"] = "built it"
        s1 = self.run_pass()
        self.assertEqual(s1["reflected_done"], 1)
        self.assertEqual(self.plane.state_changes, [("i1", "s-completed")])
        self.assertEqual(len(self.plane.comments), 1)
        self.assertIn("built it", self.plane.comments[0][1])
        # repeat passes must not write again
        for _ in range(3):
            self.run_pass()
        self.assertEqual(len(self.plane.state_changes), 1)
        self.assertEqual(len(self.plane.comments), 1)

    def test_blocked_reflects_comment_only(self):
        self.run_pass()
        tid = self.kanban.created[1]["id"]           # task for i3
        self.kanban.tasks[tid]["status"] = "blocked"
        s = self.run_pass()
        self.assertEqual(s["reflected_blocked"], 1)
        self.assertEqual(self.plane.state_changes, [])   # blocked: no state change
        self.assertEqual(len(self.plane.comments), 1)
        self.run_pass()
        self.assertEqual(len(self.plane.comments), 1)

    def test_blocked_then_done_both_reflect(self):
        self.run_pass()
        tid = self.kanban.created[0]["id"]
        self.kanban.tasks[tid]["status"] = "blocked"
        self.run_pass()
        self.kanban.tasks[tid]["status"] = "done"
        s = self.run_pass()
        self.assertEqual(s["reflected_done"], 1)
        self.assertEqual(self.plane.state_changes, [("i1", "s-completed")])
        self.assertEqual(len(self.plane.comments), 2)   # one per transition


class HelpersTest(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(html_to_text("<p>hello <b>world</b></p>"), "hello world")
        self.assertIn("- item", html_to_text("<ul><li>item</li></ul>"))

    def test_assignee_falls_back_to_default(self):
        cfg = {"assignee_map": {}, "default_assignee": "fallback-profile"}
        got = assignee_for({"assignees": [{"display_name": "unknown"}]}, cfg)
        self.assertEqual(got, "fallback-profile")


class LockTest(unittest.TestCase):
    def test_fresh_lock_blocks_second_holder(self):
        import os
        from planesync.__main__ import Lock, LockHeld
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sync.lock"
            with Lock(p):
                self.assertTrue(p.exists())
                with self.assertRaises(LockHeld):
                    with Lock(p):        # body must NOT run while held
                        self.fail("body ran while another instance held the lock")
            self.assertFalse(p.exists())  # released on exit

    def test_stale_lock_is_stolen(self):
        import os
        import time
        from planesync.__main__ import Lock
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sync.lock"
            p.write_text("999999")
            old = time.time() - 3600
            os.utime(p, (old, old))
            with Lock(p):
                self.assertTrue(p.exists())
            self.assertFalse(p.exists())


if __name__ == "__main__":
    unittest.main()
