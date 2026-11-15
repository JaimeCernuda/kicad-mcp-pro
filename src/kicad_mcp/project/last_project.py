"""Remember the active project across server restarts.

The active project lives in the server's memory, so a host that restarts the server (an
idle stdio server reaped, a configuration change) loses it, and the next tool answers
"No schematic file is configured". ``kicad_set_project`` therefore records the project
it set here, keyed by the server's working folder and workspace root, and a fresh server
whose environment names no project picks the recorded one up again.

The record is one small JSON file in ``KICAD_MCP_STATE_DIR``, else
``$XDG_STATE_HOME/kicad-mcp``, else ``~/.local/state/kicad-mcp``. Nothing is restored
when the recorded folder is gone, and a failure to read or write the record never fails
a tool: the project is then only lost, as before.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

STATE_ENV = "KICAD_MCP_STATE_DIR"
FILE_NAME = "last-project.json"
MAX_ENTRIES = 64
_FIELDS = ("project_dir", "project_file", "pcb_file", "sch_file", "output_dir")

_log = logging.getLogger(__name__)


def state_dir() -> Path:
    """The folder holding the record."""
    explicit = os.environ.get(STATE_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "kicad-mcp"


def _key(workspace_root: Path | None) -> str:
    here = str(Path.cwd().resolve())
    return f"{here}|{workspace_root.resolve() if workspace_root else ''}"


def _read(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def remember(paths: dict[str, Path | None], workspace_root: Path | None) -> None:
    """Record the project just set for this working folder."""
    path = state_dir() / FILE_NAME
    entries = _read(path)
    key = _key(workspace_root)
    entries.pop(key, None)
    entries[key] = {name: str(value) for name, value in paths.items() if value is not None}
    while len(entries) > MAX_ENTRIES:
        entries.pop(next(iter(entries)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _log.warning("last_project_not_recorded", extra={"error": str(exc)})


def recall(workspace_root: Path | None) -> dict[str, Path] | None:
    """The project recorded for this working folder, when its folder still exists."""
    entry = _read(state_dir() / FILE_NAME).get(_key(workspace_root))
    if not isinstance(entry, dict) or not entry.get("project_dir"):
        return None
    found = {name: Path(entry[name]) for name in _FIELDS if isinstance(entry.get(name), str)}
    if not found["project_dir"].is_dir():
        return None
    for name in ("project_file", "pcb_file", "sch_file"):
        if name in found and not found[name].is_file():
            found.pop(name)
    return found
