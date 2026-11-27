"""DRC on the board as it is now, not as it was last saved.

Edits through KiCad's IPC (moves, routing, zone fills) live in the running KiCad until the
board is saved; ``kicad-cli pcb drc`` reads the file. A DRC run straight after a move then
checked the old placement and reported the board clean while the moved part's tracks no
longer reached its pads. When a running KiCad serves this project's board, DRC runs on a
copy of that board in a temporary folder, beside copies of the project and rule files so
the same design rules apply. The file itself is not saved or touched.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Where the checked board came from, for the report's answer.
LIVE = "live KiCad"
FILE = "board file"


def _live_text(pcb_file: Path) -> str | None:
    """The running KiCad's text of this board, or ``None`` (none running, another board)."""
    from ..connection import _get_board  # noqa: PLC0415 - never starts KiCad

    try:
        board = _get_board()
        if Path(str(getattr(board, "name", "") or "")).name not in ("", pcb_file.name):
            return None
        return str(board.get_as_string())
    except Exception:  # noqa: BLE001 - no live board: the file is the board as it is
        return None


@contextmanager
def board_as_it_is(pcb_file: Path) -> Iterator[tuple[Path, str]]:
    """Yield the board file to check and where it came from (:data:`LIVE` or :data:`FILE`)."""
    text = _live_text(pcb_file)
    if text is None:
        yield pcb_file, FILE
        return
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-drc-") as tmp:
        copy = Path(tmp) / pcb_file.name
        copy.write_text(text, encoding="utf-8")
        for suffix in (".kicad_pro", ".kicad_dru"):
            rules = pcb_file.with_suffix(suffix)
            if rules.is_file():
                shutil.copy2(rules, Path(tmp) / rules.name)
        yield copy, LIVE
