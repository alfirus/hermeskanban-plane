"""Sync engine: one pass of both directions, with the one-way write rules.

Plane -> kanban : create-only (idempotent).
kanban -> Plane : reflect-only (state on done, comment on done/blocked).

See docs/adr/0001 and the module docstring in __init__.py.
"""

import html
import logging
import re

from .kanban import KanbanTimeout
from .plane import PlaneError

log = logging.getLogger("planesync")

TERMINAL_KANBAN = ("done", "blocked")


def html_to_text(markup: str) -> str:
    """Minimal HTML -> plain text for carrying Plane descriptions into card bodies."""
    if not markup:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", markup)
    text = re.sub(r"</(p|div|li|h[1-6]|tr|blockquote)>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def state_info(item: dict, state_map: dict | None = None) -> dict:
    """Normalize item['state'] — the API returns a plain state id (string) on
    work-items, while richer payloads embed a state object."""
    st = item.get("state")
    if isinstance(st, dict):
        return st
    if isinstance(st, str):
        if state_map and st in state_map:
            return state_map[st]
        return {"id": st, "name": "?", "group": None}  # group None = unknown
    return {"id": None, "name": "?", "group": None}


def item_state_group(item: dict, state_map: dict | None = None) -> str | None:
    return state_info(item, state_map).get("group")


def build_task_body(item: dict, project_name: str, base_url: str, workspace: str,
                    project_id: str, state_map: dict | None = None,
                    identifier: str | None = None) -> str:
    url = f"{base_url}/{workspace}/projects/{project_id}/work-items/{item['id']}"
    desc = html_to_text(item.get("description_html") or item.get("description") or "")
    state_name = state_info(item, state_map).get("name", "?")
    ident = (identifier or "").strip()
    project_line = (f"Project: {project_name} ({ident})" if ident
                    else f"Project: {project_name}")
    header = (
        f"{project_line}\n\n"
        f"Synced from Plane (source of truth for scope/status).\n\n"
        f"- Plane project: {project_name} (workspace {workspace})\n"
        f"- Work item: {item.get('name')} (#{item.get('sequence_id', '?')})\n"
        f"- State: {state_name}\n"
        f"- URL: {url}\n"
    )
    return header + ("\n---\n\n" + desc if desc else "")


def assignee_for(item: dict, cfg: dict) -> str | None:
    """Map Plane assignees to hermes profiles via config; None = unassigned."""
    amap = cfg.get("assignee_map") or {}
    names = []
    for a in item.get("assignees") or []:
        if isinstance(a, dict):
            for key in ("display_name", "email", "name"):
                if a.get(key):
                    names.append(a[key])
                    break
        elif isinstance(a, str):
            names.append(a)
    for n in names:
        if n in amap:
            return amap[n]
        local = (n.split("@")[0] if "@" in n else n)
        if local in amap:
            return amap[local]
    return cfg.get("default_assignee")


def priority_for(item: dict, cfg: dict) -> int | None:
    pmap = cfg.get("priority_map") or {}
    return pmap.get((item.get("priority") or "none").lower())


def run_sync(cfg, plane, kanban, store, dry_run: bool = False) -> dict:
    """One sync pass. Returns a summary dict; raises nothing for per-item errors
    (they are logged and counted) — only fatal setup errors propagate."""
    summary = {
        "created": 0, "skipped_existing": 0, "skipped_terminal": 0,
        "skipped_unknown_state": 0, "skipped_unmapped": 0,
        "reflected_done": 0, "reflected_blocked": 0,
        "errors": 0, "cli_wedged": False, "projects": [],
    }
    skip_groups = set(cfg.get("skip_state_groups") or [])
    completed_state_cache: dict = {}
    wedged = False  # a CLI timeout means every later call would burn its budget too

    # Resolve mapped Plane projects (config keys may be name, slug or id) once per pass.
    projects = []
    try:
        known = {}
        for p in plane.list_projects():
            for k in (p.get("name"), p.get("slug"), p.get("id")):
                if k:
                    known[k] = p
    except PlaneError as e:
        log.error("cannot list Plane projects: %s", e)
        summary["errors"] += 1
        return summary

    for name, pcfg in (cfg.get("projects") or {}).items():
        if isinstance(pcfg, dict) and pcfg.get("enabled") is False:
            continue
        p = known.get(name)
        if not p:
            log.warning("mapped Plane project not found (yet): %s", name)
            summary["skipped_unmapped"] += 1
            continue
        projects.append((name, p, pcfg if isinstance(pcfg, dict) else {}))

    # ---------- Direction 1: Plane -> kanban (create only) ----------
    for name, proj, pcfg in projects:
        if wedged:
            break
        pid = proj["id"]
        proj_kanban_board = pcfg.get("board")  # None -> sync's default board
        try:
            items = plane.list_work_items(pid)
        except PlaneError as e:
            log.error("list work items failed for %s: %s", name, e)
            summary["errors"] += 1
            continue
        summary["projects"].append({"name": name, "id": pid, "items": len(items)})

        kcli = kanban
        if proj_kanban_board and proj_kanban_board != cfg["kanban"].get("board"):
            from .kanban import KanbanClient
            kcli = KanbanClient(cli=kanban.cli, board=proj_kanban_board,
                                created_by=kanban.created_by)

        # state id -> {name, group}; work-items return state as a bare id
        try:
            state_map = {s["id"]: s for s in plane.list_states(pid)}
        except Exception as e:  # noqa: BLE001 - conservative: unknown state = no create
            log.warning("cannot list states for %s: %s", name, e)
            state_map = {}

        for item in items:
            if wedged:
                break
            issue_id = item["id"]
            if store.get(issue_id):
                summary["skipped_existing"] += 1
                continue
            group = item_state_group(item, state_map)
            if group is None:
                log.warning("state group unknown for %s; deferring creation", issue_id)
                summary["skipped_unknown_state"] += 1
                continue
            if group in skip_groups:
                summary["skipped_terminal"] += 1
                continue
            if dry_run:
                log.info("[dry-run] would create task for %s (%s)", item.get("name"), issue_id)
                summary["created"] += 1
                continue
            ident = (proj.get("identifier") or "").strip()
            body = build_task_body(item, name, cfg["plane"]["base_url"],
                                   cfg["plane"]["workspace"], pid, state_map,
                                   identifier=ident)
            raw_title = item.get("name") or "(untitled Plane item)"
            title = f"[{ident}] {raw_title}" if ident else raw_title
            try:
                task = kcli.create_task(
                    title=title,
                    body=body,
                    assignee=assignee_for(item, {**cfg, **pcfg}),
                    priority=priority_for(item, cfg),
                    idempotency_key=f"plane:{issue_id}",
                )
            except KanbanTimeout as e:
                log.error("create task failed for %s: %s", issue_id, e)
                summary["errors"] += 1
                wedged = True
                break
            except Exception as e:  # noqa: BLE001 - count and continue
                log.error("create task failed for %s: %s", issue_id, e)
                summary["errors"] += 1
                continue
            store.insert_link(issue_id, pid, name, task.get("id"),
                              kanban_status=task.get("status"))
            summary["created"] += 1
            log.info("created kanban task %s for Plane item %s (%s)",
                     task.get("id"), issue_id, item.get("name"))

    # ---------- Direction 2: kanban -> Plane (reflect only) ----------
    for link in store.all():
        if wedged:
            break
        task_id = link.get("kanban_task_id")
        if not task_id:
            continue
        try:
            task = kanban.show(task_id)
        except KanbanTimeout as e:  # noqa: BLE001
            log.error("kanban show failed for %s: %s", task_id, e)
            summary["errors"] += 1
            wedged = True
            break
        except Exception as e:  # noqa: BLE001
            log.error("kanban show failed for %s: %s", task_id, e)
            summary["errors"] += 1
            continue
        status = (task.get("status") or "").lower()
        store.update_status(link["plane_issue_id"], status)
        if status not in TERMINAL_KANBAN:
            continue
        if link.get("reflected_status") == status:
            continue  # already reflected this transition - no repeats, no loops

        summary_key = "reflected_done" if status == "done" else "reflected_blocked"
        if dry_run:
            log.info("[dry-run] would reflect %s on Plane item %s", status, link["plane_issue_id"])
            summary[summary_key] += 1
            continue

        run_summary = _task_run_summary(task)
        try:
            if status == "done":
                state_id = plane.completed_state_id(link["plane_project_id"], completed_state_cache)
                if state_id:
                    plane.set_work_item_state(link["plane_project_id"],
                                              link["plane_issue_id"], state_id)
                else:
                    log.warning("no completed-group state on project %s; comment only",
                                link["plane_project_name"])
            plane.add_comment(
                link["plane_project_id"], link["plane_issue_id"],
                comment_html=_reflection_html(task_id, status, run_summary, task),
                external_id=task_id,
            )
        except PlaneError as e:
            if e.status == 404:
                log.error("Plane item %s gone (orphan link %s)", link["plane_issue_id"], task_id)
            else:
                log.error("reflect %s to Plane failed for %s: %s", status, task_id, e)
            summary["errors"] += 1
            continue
        store.mark_reflected(link["plane_issue_id"], status)
        summary[summary_key] += 1
        log.info("reflected %s to Plane item %s (task %s)", status, link["plane_issue_id"], task_id)

    if wedged:
        summary["cli_wedged"] = True
        log.warning("kanban CLI timed out — skipping the rest of this pass "
                    "(the next tick retries; see the timeout line above for the cause)")

    return summary


def _task_run_summary(task: dict) -> str:
    for run in reversed(task.get("_runs") or []):
        if run.get("summary"):
            return str(run["summary"]).strip()
    result = task.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return "(no summary recorded)"


def _reflection_html(task_id: str, status: str, run_summary: str, task: dict) -> str:
    esc = lambda s: html.escape(str(s))  # noqa: E731
    lines = (
        f"<p><strong>[hermes-sync]</strong> Kanban task <code>{esc(task_id)}</code> "
        f"went <strong>{esc(status)}</strong>.</p>"
        f"<p>Agent summary: {esc(run_summary)}</p>"
        f"<p><em>Reflected automatically by the Plane↔Hermes-Kanban sync.</em></p>"
    )
    return lines
