"""A private ``KICAD_CONFIG_HOME`` for a KiCad the server starts.

KiCad shows a first-run wizard, which blocks its IPC API ("KiCad is not ready to
reply"), when its configuration folder lacks the global library tables. KiCad has no
command-line flag or environment variable that skips the wizard; it is skipped by
seeding the folder. This module seeds, under ``<home>/<major.minor>/``:

* ``kicad_common.json`` with ``api.enable_server`` on and the first-run prompts
  (data collection, update check) marked as answered;
* ``sym-lib-table`` and ``fp-lib-table`` with the libraries of KiCad's template folder
  that the project's design uses, or empty tables when it cannot be found. KiCad loads
  every library a global table lists as soon as a check or the editor needs one; the
  full template (about 380 libraries) costs a DRC or ERC 1.4-1.9 GB, the design's own
  libraries a few hundred MB, with the same findings;
* ``design-block-lib-table``, an empty table (KiCad 10 ships no template for it).

Existing settings are kept, except that ``api.enable_server`` is turned back on; the
tables follow the design each time the folder is seeded.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: A quoted ``"nickname:item"`` in a design: a footprint, symbol or Footprint property.
_LIBRARY_REFERENCE = re.compile(r'"([^":\s]+):[^"]+"')
_TABLE_ENTRY = re.compile(r'\s*\(lib\s+\(name\s+"?([^")\s]+)')

#: The settings key that turns on KiCad's IPC API server.
API_SETTING = ("api", "enable_server")

_EMPTY_TABLES = {
    "sym-lib-table": "(sym_lib_table\n\t(version 7)\n)\n",
    "fp-lib-table": "(fp_lib_table\n\t(version 7)\n)\n",
    "design-block-lib-table": "(design_block_lib_table\n\t(version 7)\n)\n",
}

_PROMPTS_ANSWERED = ("data_collection_prompt", "update_check_prompt")


def settings_version(version_text: str | None) -> str:
    """The settings folder name for a KiCad version: ``10.0.8`` gives ``10.0``."""
    match = re.search(r"(\d+)\.(\d+)", version_text or "")
    return f"{match.group(1)}.{match.group(2)}" if match else "10.0"


def template_dirs(cli: Path | None) -> list[Path]:
    """Where KiCad's template folder may be, nearest to the CLI first."""
    found: list[Path] = []
    if cli is not None:
        resolved = cli.resolve()
        for root in (resolved.parent.parent, resolved.parent.parent.parent):
            found.append(root / "share" / "kicad" / "template")
            found.append(root / "SharedSupport" / "template")  # macOS bundle
    found.extend(
        [
            Path("/usr/share/kicad/template"),
            Path("/usr/local/share/kicad/template"),
            Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/template"),
        ]
    )
    return found


def used_libraries(project_dir: Path | None) -> set[str]:
    """The library nicknames the boards and schematics in ``project_dir`` name."""
    names: set[str] = set()
    if project_dir is None:
        return names
    for design in (*project_dir.rglob("*.kicad_pcb"), *project_dir.rglob("*.kicad_sch")):
        if any(part.startswith(".") for part in design.relative_to(project_dir).parts):
            continue
        try:
            names.update(_LIBRARY_REFERENCE.findall(design.read_text("utf-8", errors="ignore")))
        except OSError:
            continue
    return names


def seed(
    home: Path,
    version_text: str | None,
    cli: Path | None = None,
    project_dir: Path | None = None,
) -> Path:
    """Seed ``home`` for this KiCad version and ``project_dir``; return the version folder."""
    folder = home / settings_version(version_text)
    folder.mkdir(parents=True, exist_ok=True)
    common = folder / "kicad_common.json"
    data: dict[str, object] = {}
    if common.is_file():
        try:
            loaded = json.loads(common.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            data = {}
    api = data.setdefault("api", {})
    if isinstance(api, dict):
        api["enable_server"] = True
    prompts = data.setdefault("do_not_show_again", {})
    if isinstance(prompts, dict):
        for key in _PROMPTS_ANSWERED:
            prompts.setdefault(key, True)
    _write(common, json.dumps(data, indent=2))
    template = next((d for d in template_dirs(cli) if (d / "sym-lib-table").is_file()), None)
    used = used_libraries(project_dir)
    for name, empty in _EMPTY_TABLES.items():
        source = template / name if template is not None else None
        text = source.read_text(encoding="utf-8") if source and source.is_file() else empty
        kept = (line for line in text.splitlines(keepends=True) if _keeps(line, used))
        _write(folder / name, "".join(kept))
    return folder


def _keeps(line: str, used: set[str]) -> bool:
    entry = _TABLE_ENTRY.match(line)
    return entry is None or entry.group(1) in used


def _write(path: Path, text: str) -> None:
    """Write ``text`` unless the file already holds it (a running KiCad may be reading it)."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")


def api_enabled(home: Path, version_text: str | None) -> bool:
    """Whether the seeded settings have the API server on."""
    common = home / settings_version(version_text) / "kicad_common.json"
    try:
        data = json.loads(common.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and (data.get("api") or {}).get("enable_server"))
