"""A board or schematic cut short is named as such, not opened in KiCad or drawn empty."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.instance import manager as instance_manager
from kicad_mcp.instance.board_file import file_problem
from kicad_mcp.instance.manager import InstanceManager
from kicad_mcp.views import data

CUT = '(kicad_pcb\n  (version 20240101)\n  (generator "x")\n  (layers\n'


def test_complete_files_pass_including_parentheses_inside_strings(tmp_path: Path) -> None:
    board = tmp_path / "ok.kicad_pcb"
    board.write_text('(kicad_pcb (version 1) (gr_text "a ( b \\" ) c" (at 0 0)))\n')
    sheet = tmp_path / "ok.kicad_sch"
    sheet.write_text('(kicad_sch (version 1) (property "Value" "10k (1%)"))')
    assert file_problem(board) == "" and file_problem(sheet) == ""


@pytest.mark.parametrize(
    ("text", "words"),
    [
        (CUT, "ends with 2 unclosed parentheses"),
        ('(kicad_pcb (gr_text "cut', "ends inside a quoted value"),
        ("(kicad_pcb))", "closes more parentheses than it opens"),
        ("(kicad_sch)", "does not start with (kicad_pcb"),
    ],
)
def test_damaged_files_say_what_is_wrong(tmp_path: Path, text: str, words: str) -> None:
    board = tmp_path / "led.kicad_pcb"
    board.write_text(text)
    assert words in file_problem(board)


def test_kicad_is_not_started_on_a_board_cut_short(monkeypatch, tmp_path: Path) -> None:
    board = tmp_path / "led.kicad_pcb"
    board.write_text(CUT)
    monkeypatch.setenv(instance_manager.INSTANCE_ENV, "auto")
    monkeypatch.setattr(instance_manager, "probe", lambda url: instance_manager.Probe(False))
    started: list[object] = []
    monkeypatch.setattr(InstanceManager, "_start", lambda *args: started.append(args))
    status = InstanceManager().ensure(board)
    assert not started
    assert status.problem.startswith("KiCad was not started: led.kicad_pcb is incomplete")
    assert "-backups" in status.next_step


def test_a_kicad_that_already_has_the_board_open_is_still_attached(
    monkeypatch, tmp_path: Path
) -> None:
    # Its copy in memory is whole even when the file on disk was cut short.
    board = tmp_path / "led.kicad_pcb"
    board.write_text(CUT)
    monkeypatch.setenv(instance_manager.INSTANCE_ENV, "auto")
    monkeypatch.setattr(
        instance_manager,
        "probe",
        lambda url: instance_manager.Probe(True, True, "10.0.8", (str(board),)),
    )
    assert InstanceManager().ensure(board).case == "attached"


def test_the_views_name_a_file_cut_short(
    sample_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp.config import get_config

    (sample_project / "demo.kicad_pcb").write_text(CUT)
    (sample_project / "demo.kicad_sch").write_text("(kicad_sch (symbol\n")
    get_config().apply_project(sample_project)
    monkeypatch.setattr(data, "live_board_text", lambda: None)
    with pytest.raises(ValueError, match=r"demo.kicad_pcb is incomplete.*-backups"):
        data.board_data()
    with pytest.raises(ValueError, match="demo.kicad_sch is incomplete"):
        data.schematic_data()
