"""Config loading: mapping JSON (committed, non-secret) + token from secrets file."""

import json
import os
from pathlib import Path


def repo_root() -> Path:
    """Repository root = two levels up from src/planesync/."""
    return Path(__file__).resolve().parents[2]


def default_config_path() -> Path:
    return repo_root() / "config" / "mapping.json"


def load_env_file(path: str) -> dict:
    """Parse a KEY=VALUE env file (comments allowed). Values never logged."""
    out = {}
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path) if path else default_config_path()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    plane = cfg.setdefault("plane", {})
    plane.setdefault("base_url", "https://plane.alfirus.my")
    plane.setdefault("workspace", "aerosgeotech")
    plane.setdefault("api_token_env_file", "~/.hermes-secrets/plane-sync.env")
    plane.setdefault("api_token_var", "PLANE_API_TOKEN")

    kanban = cfg.setdefault("kanban", {})
    kanban.setdefault("cli", "hermes")
    kanban.setdefault("board", None)
    kanban.setdefault("created_by", "planesync")

    cfg.setdefault("assignee_map", {})
    cfg.setdefault("priority_map", {
        "urgent": 90, "high": 70, "medium": 50, "low": 30, "none": 10,
    })
    cfg.setdefault("default_assignee", None)
    cfg.setdefault("skip_state_groups", ["completed", "cancelled"])
    cfg.setdefault("data_dir", "~/.hermes/planesync")
    cfg.setdefault("projects", {})

    cfg["_config_path"] = str(cfg_path)
    return cfg


def resolve_plane_token(cfg: dict) -> str:
    """Read the API token from the secrets env file. Never log the value."""
    plane = cfg["plane"]
    env = load_env_file(plane["api_token_env_file"])
    token = env.get(plane["api_token_var"]) or os.environ.get(plane["api_token_var"], "")
    if not token:
        raise RuntimeError(
            "Plane API token not found: expected {} in {}".format(
                plane["api_token_var"], plane["api_token_env_file"]
            )
        )
    return token


def data_dir(cfg: dict) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(cfg["data_dir"])))
    p.mkdir(parents=True, exist_ok=True)
    return p
