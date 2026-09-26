"""Thin Plane REST client (self-hosted, /api/v1, X-API-Key). Stdlib only."""

import json
import urllib.error
import urllib.parse
import urllib.request


class PlaneError(RuntimeError):
    def __init__(self, status, path, body):
        super().__init__(f"Plane API {status} on {path}: {body[:300]}")
        self.status = status
        self.path = path
        self.body = body


class PlaneClient:
    def __init__(self, base_url: str, workspace: str, token: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.workspace = workspace
        self.token = token
        self.timeout = timeout

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, path: str, payload=None, params=None):
        url = f"{self.base}/api/v1/workspaces/{urllib.parse.quote(self.workspace)}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", self.token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (401, 403):
                raise PlaneError(e.code, path, body) from None
            return e.code, body

    def _paged(self, path: str, params=None):
        """GET a collection. Handles both plain lists and cursor envelopes."""
        items, cursor = [], None
        while True:
            p = dict(params or {})
            if cursor:
                p["cursor"] = cursor
            status, body = self._request("GET", path, params=p)
            if status != 200:
                raise PlaneError(status, path, body)
            data = json.loads(body)
            if isinstance(data, list):
                items.extend(data)
                break
            items.extend(data.get("results") or [])
            if data.get("next_page_results") and data.get("next_cursor"):
                cursor = data["next_cursor"]
                if len(items) > 20000:  # safety valve
                    break
            else:
                break
        return items

    # -- resources ---------------------------------------------------------
    def list_projects(self):
        return self._paged("/projects/")

    def project_by_name(self, name: str):
        for p in self.list_projects():
            if p.get("name") == name or p.get("slug") == name:
                return p
        return None

    def list_states(self, project_id: str):
        return self._paged(f"/projects/{project_id}/states/")

    def list_work_items(self, project_id: str, per_page: int = 100):
        return self._paged(f"/projects/{project_id}/work-items/",
                           params={"per_page": per_page, "expand": "assignees"})

    def get_work_item(self, project_id: str, item_id: str):
        status, body = self._request("GET", f"/projects/{project_id}/work-items/{item_id}/")
        if status != 200:
            raise PlaneError(status, f"work-items/{item_id}", body)
        return json.loads(body)

    def set_work_item_state(self, project_id: str, item_id: str, state_id: str):
        status, body = self._request(
            "PATCH", f"/projects/{project_id}/work-items/{item_id}/", payload={"state": state_id}
        )
        if status != 200:
            raise PlaneError(status, f"PATCH work-items/{item_id}", body)
        return json.loads(body)

    def add_comment(self, project_id: str, item_id: str, comment_html: str,
                    external_id: str | None = None, external_source: str = "hermes-kanban"):
        payload = {"comment_html": comment_html, "external_source": external_source}
        if external_id:
            payload["external_id"] = external_id
        status, body = self._request(
            "POST", f"/projects/{project_id}/work-items/{item_id}/comments/", payload=payload
        )
        if status != 201:
            raise PlaneError(status, f"comments/{item_id}", body)
        return json.loads(body)

    def completed_state_id(self, project_id: str, cache: dict) -> str | None:
        """First state in the 'completed' group for this project (cached)."""
        if project_id in cache:
            return cache[project_id]
        for s in self.list_states(project_id):
            if s.get("group") == "completed":
                cache[project_id] = s["id"]
                return s["id"]
        cache[project_id] = None
        return None
