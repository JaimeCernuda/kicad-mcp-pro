"""DRC checks the board as it is now: a running KiCad's unsaved edits included."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp import connection
from kicad_mcp.validation import live_board


class _Board:
    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self._text = text

    def get_as_string(self) -> str:
        return self._text


def _project(tmp_path: Path) -> Path:
    pcb = tmp_path / "led.kicad_pcb"
    pcb.write_text("(kicad_pcb (footprint R1 (at 10 10)))", encoding="utf-8")
    (tmp_path / "led.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "led.kicad_dru").write_text("(version 1)", encoding="utf-8")
    return pcb


def test_a_live_board_is_checked_from_a_copy_beside_its_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcb = _project(tmp_path)
    moved = "(kicad_pcb (footprint R1 (at 30 10)))"
    monkeypatch.setattr(connection, "_get_board", lambda: _Board("led.kicad_pcb", moved))
    with live_board.board_as_it_is(pcb) as (checked, source):
        assert source == live_board.LIVE
        assert checked != pcb and checked.name == pcb.name
        assert checked.read_text(encoding="utf-8") == moved
        assert (checked.parent / "led.kicad_dru").read_text(encoding="utf-8") == "(version 1)"
        assert (checked.parent / "led.kicad_pro").is_file()
        folder = checked.parent
    assert not folder.exists()  # the copy is gone; the board file was never touched
    assert "(at 10 10)" in pcb.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "board",
    [
        pytest.param(lambda: _Board("other.kicad_pcb", "(kicad_pcb)"), id="another board"),
        pytest.param(None, id="no KiCad"),
    ],
)
def test_without_a_live_copy_of_this_board_the_file_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, board
) -> None:
    pcb = _project(tmp_path)

    def unreachable() -> _Board:
        raise connection.KiCadConnectionError("no KiCad")

    monkeypatch.setattr(connection, "_get_board", board or unreachable)
    with live_board.board_as_it_is(pcb) as (checked, source):
        assert (checked, source) == (pcb, live_board.FILE)
