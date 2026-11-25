"""Footprint, layer and render inputs the way an agent passes them."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_mcp.models.pcb import AddTrackInput, AddZoneInput
from kicad_mcp.tools import export_support, footprint, schematic

_OLD_FOOTPRINT = """(footprint "Ant" (version 20211014) (generator pcbnew) (layer "F.Cu")
  (descr "Printed antenna") (tags "rf antenna") (attr smd)
  (fp_text reference "REF**" (at 0 0) (layer "F.SilkS"))
  (fp_line (start 0 0) (end 1 0) (layer "F.CrtYd") (width 0.05))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
  (pad "2" thru_hole circle (at 2 0) (size 1 1) (drill 0.5) (layers "*.Cu"))
  (model "${KIPRJMOD}/ant.step")
)
"""


def _config(project_dir: Path, **overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {"project_dir": project_dir, "pcb_file": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_fp_get_info_reads_a_footprint_in_a_library(monkeypatch, tmp_path: Path) -> None:
    library = tmp_path / "Lib.pretty"
    library.mkdir()
    (library / "Ant.kicad_mod").write_text(_OLD_FOOTPRINT, encoding="utf-8")
    monkeypatch.setattr(footprint, "get_config", lambda: _config(tmp_path))

    listing = footprint.fp_get_info(input_path="Lib.pretty")
    assert "- Ant" in listing

    info = footprint.fp_get_info(input_path="Lib.pretty", footprint="Ant")
    assert "Footprint: Ant" in info
    assert "Description: Printed antenna" in info
    assert "Pads: 2 (1 smd, 1 thru_hole); numbered: 1, 2" in info
    assert "Courtyard: yes" in info
    assert "${KIPRJMOD}/ant.step" in info
    assert "in_place=True" in info


def test_fp_get_info_lists_the_boards_footprints(monkeypatch, tmp_path: Path) -> None:
    board = tmp_path / "b.kicad_pcb"
    board.write_text(
        '(kicad_pcb\n\t(footprint "R:R_0402"\n\t)\n\t(footprint "R:R_0402"\n\t)'
        '\n\t(footprint "Lib:Ant"\n\t)\n)',
        encoding="utf-8",
    )
    monkeypatch.setattr(footprint, "get_config", lambda: _config(tmp_path, pcb_file=board))

    result = footprint.fp_get_info()
    assert "3 placed, 2 kinds" in result
    assert "- R:R_0402 x2" in result


def test_fp_upgrade_dry_run_says_how_to_write(monkeypatch, tmp_path: Path) -> None:
    library = tmp_path / "Lib.pretty"
    library.mkdir()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(footprint, "get_config", lambda: _config(tmp_path))
    monkeypatch.setattr(footprint, "_run_cli", lambda *cmd: calls.append(cmd) or (0, "", ""))

    preview = footprint.fp_upgrade(input_path="Lib.pretty", force=True)
    assert "Nothing was written" in preview
    assert "dry_run=False" in preview
    assert not calls

    done = footprint.fp_upgrade(input_path="Lib.pretty", force=True, dry_run=False, in_place=True)
    assert calls == [("fp", "upgrade", "--force", str(library))]
    assert str(library) in done


def test_fp_export_svg_takes_one_layer_or_a_comma_list(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "a.kicad_mod").write_text(_OLD_FOOTPRINT, encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(footprint, "get_config", lambda: _config(tmp_path))
    monkeypatch.setattr(footprint, "_ensure_output_dir", lambda _name: tmp_path)
    monkeypatch.setattr(footprint, "_run_cli", lambda *cmd: calls.append(cmd) or (0, "", ""))

    footprint.fp_export_svg(input_path="a.kicad_mod", layers="F.Cu")
    footprint.fp_export_svg(input_path="a.kicad_mod", layers="F.Cu, F.SilkS")
    footprint.fp_export_svg(input_path="a.kicad_mod", layers=["F.Cu", "F.SilkS"])

    layer_args = [cmd[cmd.index("--layers") + 1] for cmd in calls]
    assert layer_args == ["F.Cu", "F.Cu,F.SilkS", "F.Cu,F.SilkS"]


def test_kicads_own_layer_names_are_accepted() -> None:
    corners = [{"x_mm": 0, "y_mm": 0}, {"x_mm": 1, "y_mm": 0}, {"x_mm": 1, "y_mm": 1}]
    assert AddZoneInput(net_name="GND", layer="B.Cu", corners=corners).layer == "B_Cu"
    assert AddZoneInput(net_name="GND", layer="In1.Cu", corners=corners).layer == "In1_Cu"
    assert AddTrackInput(x1_mm=0, y1_mm=0, x2_mm=1, y2_mm=1, layer="F_Cu").layer == "F_Cu"


def test_a_render_path_error_names_the_render_dir(monkeypatch, tmp_path: Path) -> None:
    render_dir = tmp_path / "schematic-renders"
    monkeypatch.setattr(export_support, "_ensure_output_dir", lambda _name: render_dir)

    with pytest.raises(ValueError) as error:
        schematic._safe_render_output_path(r"D:\elsewhere\x.png", default_name="board.png")

    assert str(render_dir) in str(error.value)
    assert "'board.png'" in str(error.value)
