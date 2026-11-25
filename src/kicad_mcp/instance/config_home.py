"""A private ``KICAD_CONFIG_HOME`` for a KiCad the server starts.

KiCad shows a first-run wizard, which blocks its IPC API ("KiCad is not ready to
reply"), when its configuration folder lacks the global library tables. KiCad has no
command-line flag or environment variable that skips the wizard; it is skipped by
seeding the folder. This module seeds, under ``<home>/<major.minor>/``:

* ``kicad_common.json`` with ``api.enable_server`` on and the first-run prompts
  (data collection, update check) marked as answered;
* ``sym-lib-table`` and ``fp-lib-table`` copied from KiCad's template folder, or
  empty tables when it cannot be found;
* ``design-block-lib-table``, an empty table (KiCad 10 ships no template for it).

Existing files are kept, except that ``api.enable_server`` is turned back on.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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


def seed(home: Path, version_text: str | None, cli: Path | None = None) -> Path:
    """Seed ``home`` for this KiCad version and return the version folder."""
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
    common.write_text(json.dumps(data, indent=2), encoding="utf-8")
    template = next((d for d in template_dirs(cli) if (d / "sym-lib-table").is_file()), None)
    for name, empty in _EMPTY_TABLES.items():
        target = folder / name
        source = template / name if template is not None else None
        has_source = source is not None and source.is_file()
        # An empty table seeded before KiCad was installed is replaced once its template is.
        if target.exists() and not (has_source and _is_empty_table(target, empty)):
            continue
        if source is not None and has_source:
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            target.write_text(empty, encoding="utf-8")
    return folder


def _is_empty_table(path: Path, empty: str) -> bool:
    try:
        return path.read_text(encoding="utf-8") == empty
    except OSError:
        return False


def api_enabled(home: Path, version_text: str | None) -> bool:
    """Whether the seeded settings have the API server on."""
    common = home / settings_version(version_text) / "kicad_common.json"
    try:
        data = json.loads(common.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and (data.get("api") or {}).get("enable_server"))
