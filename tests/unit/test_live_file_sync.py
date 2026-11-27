"""A file-level board edit keeps an open KiCad and the board file in step.

Found live: FreeRouting's session was written to the board file while the KiCad the connector
had started kept showing the unrouted board, so its checks said 53 connections were open and
a save from KiCad would have written the unrouted board over the routing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_mcp.errors import ToolValidationError
from kicad_mcp.pcb import live_file_sync
from kicad_mcp.tools import pcb


class _OpenKiCad:
    """KiCad with one board open: holds its own text, reloads from the file on revert."""

    def __init__(self, path: Path, text: str) -> None:
        self.name = path.name
        self._folder = str(path.parent)
        self._path = path
        self.text = text
        self.reverts = 0

    def get_project(self) -> SimpleNamespace:
        return SimpleNamespace(path=self._folder)

    def get_as_string(self) -> str:
        return self.text

    def revert(self) -> None:
        self.reverts += 1
        self.text = self._path.read_text(encoding="utf-8")


@pytest.fixture
def board_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    board = tmp_path / "sensor.kicad_pcb"
    board.write_text("(kicad_pcb (saved))\n", encoding="utf-8")
    monkeypatch.setattr(pcb, "_get_pcb_file_for_sync", lambda: board)
    monkeypatch.setattr(pcb, "_normalize_board_content", lambda text: text)
    monkeypatch.setattr(pcb, "_validate_board_text", lambda text: None)
    monkeypatch.setattr(pcb, "get_config", lambda: SimpleNamespace(workspace=tmp_path))
    monkeypatch.setattr(
        pcb, "upgrade_generated_file", lambda *a, **k: SimpleNamespace(upgraded=True)
    )
    monkeypatch.setattr(pcb, "_run_queued_ipc_mutation", lambda _op, command: command())
    monkeypatch.setattr(pcb, "_native_live_transaction_active", lambda: False)
    return board


def _kicad_has(monkeypatch: pytest.MonkeyPatch, board: object) -> None:
    import kicad_mcp.connection as connection

    monkeypatch.setattr(connection, "_get_board", lambda: board)


def test_an_edit_to_the_open_board_starts_from_kicad_and_kicad_reloads_it(
    board_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kicad = _OpenKiCad(board_file, "(kicad_pcb (saved) (moved-in-kicad))\n")
    _kicad_has(monkeypatch, kicad)

    pcb._transactional_board_write(lambda text: text.replace("))", ") (routed))"))

    written = board_file.read_text(encoding="utf-8")
    assert "(moved-in-kicad)" in written, "the unsaved change in KiCad is kept"
    assert "(routed)" in written
    assert kicad.reverts == 1
    assert kicad.text == written, "KiCad shows what the file now holds"


def test_without_kicad_the_file_is_edited_as_before(
    board_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kicad_mcp.connection as connection

    def not_running() -> object:
        raise connection.KiCadConnectionError("KiCad is not running")

    monkeypatch.setattr(connection, "_get_board", not_running)

    pcb._transactional_board_write(lambda text: text.replace("))", ") (routed))"))

    assert board_file.read_text(encoding="utf-8") == "(kicad_pcb (saved) (routed))\n"


def test_kicad_with_another_board_open_is_left_alone(
    board_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "other" / "sensor.kicad_pcb"
    elsewhere.parent.mkdir()
    elsewhere.write_text("(kicad_pcb (other))\n", encoding="utf-8")
    kicad = _OpenKiCad(elsewhere, "(kicad_pcb (other))\n")
    _kicad_has(monkeypatch, kicad)

    pcb._transactional_board_write(lambda text: text.replace("))", ") (routed))"))

    assert board_file.read_text(encoding="utf-8") == "(kicad_pcb (saved) (routed))\n"
    assert kicad.reverts == 0
    assert kicad.text == "(kicad_pcb (other))\n"


def test_an_open_kicad_commit_is_not_dropped_by_a_file_edit(
    board_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kicad = _OpenKiCad(board_file, "(kicad_pcb (saved) (staged))\n")
    _kicad_has(monkeypatch, kicad)
    monkeypatch.setattr(pcb, "_native_live_transaction_active", lambda: True)

    with pytest.raises(ToolValidationError, match="pcb_push_commit"):
        pcb._transactional_board_write(lambda text: text + "(routed)")

    assert board_file.read_text(encoding="utf-8") == "(kicad_pcb (saved))\n"
    assert kicad.reverts == 0


def test_open_board_for_never_starts_kicad(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import kicad_mcp.connection as connection

    def must_not_start() -> bool:
        raise AssertionError("a file edit must not start KiCad")

    monkeypatch.setattr(connection, "_ensure_instance", must_not_start)
    monkeypatch.setattr(
        connection,
        "_get_board",
        lambda: (_ for _ in ()).throw(connection.KiCadConnectionError("not running")),
    )

    assert live_file_sync.open_board_for(tmp_path / "x.kicad_pcb") is None


def test_a_revert_says_what_kicad_shows_once_reloaded() -> None:
    """Found live: the agent read the board beside its revert, saw the old board and decided
    the revert had failed. The revert now reports what KiCad holds after it."""
    from kicad_mcp.pcb.transaction_lifecycle import _reloaded_contents

    board = SimpleNamespace(
        get_tracks=lambda: [object()] * 215,
        get_vias=lambda: [object()] * 17,
        get_footprints=lambda: [object()] * 19,
    )
    assert _reloaded_contents(board) == (
        " KiCad now shows 215 track segment(s), 17 via(s) and 19 footprint(s)."
    )
