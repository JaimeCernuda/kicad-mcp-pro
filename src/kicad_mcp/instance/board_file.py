"""Whether a KiCad board or schematic file is complete enough to open.

A file cut short (a crash mid-save, a partial copy) still starts with its header, so
KiCad's editor opens a dialog nobody can see on a virtual display and never answers the
server, and ecad-viewer reads it as an empty board. Checking
first lets the answer say what is wrong with the file instead.
"""

from __future__ import annotations

import re
from pathlib import Path

_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_HEADERS = {".kicad_pcb": "(kicad_pcb", ".kicad_sch": "(kicad_sch"}


def file_problem(path: Path) -> str:
    """Why ``path`` cannot be a complete KiCad board or schematic, or ``""``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"{path.name} could not be read: {exc}."
    header = _HEADERS.get(path.suffix, "(kicad_")
    if not text.lstrip().startswith(header):
        return f"{path.name} is not a KiCad file: it does not start with {header}."
    bare = _STRING.sub("", text)
    if bare.count('"') % 2:
        return f"{path.name} is incomplete: it ends inside a quoted value."
    depth = 0
    for char in bare:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return f"{path.name} is damaged: it closes more parentheses than it opens."
    if depth:
        return (
            f"{path.name} is incomplete: it ends with {depth} unclosed parenthes"
            f"{'is' if depth == 1 else 'es'}, as a save cut short leaves it."
        )
    return ""
