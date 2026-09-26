"""CLI entry point: python -m planesync [--once|--loop SECS|--status] [--dry-run]."""

import argparse
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config as cfgmod
from .kanban import KanbanClient
from .plane import PlaneClient
from .store import LinkStore
from .sync import run_sync

log = logging.getLogger("planesync")


def setup_logging(data_dir: Path, verbose: bool):
    logdir = data_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    fh = RotatingFileHandler(logdir / "planesync.log", maxBytes=1_000_000,
                             backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


class LockHeld(RuntimeError):
    """Another planesync instance currently holds the sync lock."""


class Lock:
    """Prevent overlapping runs (scheduled ticks can stack behind a hung run).

    Raises LockHeld when another instance holds a fresh lock, so callers skip
    the pass entirely — a log line alone would still let the body run.
    """

    def __init__(self, path: Path, stale_after: int = 1800):
        self.path = path
        self.stale_after = stale_after
        self.acquired = False

    def __enter__(self):
        if self.path.exists():
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0
            if age < self.stale_after:
                raise LockHeld(f"another sync holds the lock ({age:.0f}s old)")
            log.warning("stale lock (%.0fs old) stealing", age)
            try:
                self.path.unlink()
            except OSError:
                pass
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
        except FileExistsError as e:
            raise LockHeld("lock race; another run started first") from e
        return self

    def __exit__(self, *exc):
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


def print_status(store: LinkStore):
    rows = store.all()
    print(f"{len(rows)} linked item(s)")
    for r in rows:
        print(f"  {r['plane_project_name'] or '?':<20} issue={r['plane_issue_id'][:8]} "
              f"task={r['kanban_task_id'] or '-':<12} kanban={r['kanban_status'] or '-':<10} "
              f"reflected={r['reflected_status'] or '-'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="planesync",
                                 description="Plane <-> Hermes Kanban sync (one pass per run)")
    ap.add_argument("--config", default=None, help="path to mapping.json")
    ap.add_argument("--once", action="store_true", help="run a single pass (default)")
    ap.add_argument("--loop", type=int, metavar="SECS", default=0,
                    help="run continuously, one pass every SECS seconds")
    ap.add_argument("--status", action="store_true", help="print link store and exit")
    ap.add_argument("--dry-run", action="store_true", help="log actions without writing")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = cfgmod.load_config(args.config)
    ddir = cfgmod.data_dir(cfg)
    setup_logging(ddir, args.verbose)
    log.info("planesync starting (config=%s dry_run=%s)", cfg["_config_path"], args.dry_run)

    store = LinkStore(ddir / "links.db")
    if args.status:
        print_status(store)
        return 0

    try:
        token = cfgmod.resolve_plane_token(cfg)
    except RuntimeError as e:
        log.error("%s", e)
        return 2

    plane = PlaneClient(cfg["plane"]["base_url"], cfg["plane"]["workspace"], token)
    kanban = KanbanClient(cli=cfg["kanban"]["cli"], board=cfg["kanban"]["board"],
                          created_by=cfg["kanban"]["created_by"])

    def one_pass() -> int:
        try:
            with Lock(ddir / "sync.lock"):
                try:
                    summary = run_sync(cfg, plane, kanban, store, dry_run=args.dry_run)
                except Exception:
                    log.exception("sync pass crashed")
                    return 1
        except LockHeld as e:
            log.info("%s; skipping this tick", e)
            return 0
        print(json.dumps({"summary": summary}))
        return 0 if summary.get("errors", 0) == 0 else 1

    rc = 0
    if args.loop:
        log.info("loop mode: every %ss", args.loop)
        while True:
            rc = one_pass()
            time.sleep(args.loop)
    else:
        rc = one_pass()
    return rc


if __name__ == "__main__":
    sys.exit(main())
