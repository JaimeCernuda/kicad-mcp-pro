"""Keep an open KiCad and the board file in step around a file-level board edit.

Some edits are made to the ``.kicad_pcb`` text (an imported FreeRouting session, a barcode,
a stackup...). When KiCad has that board open, writing the file alone leaves KiCad showing
the old board: its checks read the old board, and a later save from KiCad writes the old
board over the edit. So an edit to an open board starts from the board KiCad holds (its
unsaved changes included) and ends with KiCad reloading the file it now matches.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

if TYPE_CHECKING:
    from kipy.board import Board

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OpenBoard:
    """The KiCad board that has ``path`` open."""

    board: Board
    path: Path

    def text(self) -> str:
        """The board as KiCad holds it, unsaved changes included."""
        return self.board.get_as_string()

    def reload(self) -> None:
        """Have KiCad load the file it now matches."""
        revert = cast(Callable[[], None], self.board.revert)  # kipy leaves it untyped
        revert()


def open_board_for(board_file: Path) -> OpenBoard | None:
    """The running KiCad's board when it is ``board_file``; None when KiCad is not running
    or has another board open. Never starts KiCad."""
    from ..connection import KiCadConnectionError, _get_board  # noqa: PLC0415

    try:
        board = _get_board()
    except (KiCadConnectionError, OSError) as exc:
        logger.debug("live_file_sync_no_open_board", error=str(exc))
        return None
    try:
        name = board.name
        folder = board.get_project().path
    except Exception as exc:  # noqa: BLE001 - an unreadable document is not this board
        logger.debug("live_file_sync_board_unreadable", error=str(exc))
        return None
    if name != board_file.name:
        return None
    if folder and Path(folder).resolve() != board_file.parent.resolve():
        return None
    return OpenBoard(board=board, path=board_file)
