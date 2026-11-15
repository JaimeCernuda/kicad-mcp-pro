"""The BOM carries each symbol's own Datasheet field (the BOM view's Open link)."""

from pathlib import Path

import pytest

from kicad_mcp.export.bom import BOM_FIELDNAMES
from kicad_mcp.tools import library as library_mod


def _rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, symbols: list[dict]):
    sch_file = tmp_path / "board.kicad_sch"
    sch_file.write_text(text, encoding="utf-8")

    class FakeBackend:
        def parse_schematic_file(self, _path: Path) -> dict[str, object]:
            return {"symbols": symbols}

    monkeypatch.setattr(library_mod, "_active_schematic_file", lambda: sch_file)
    monkeypatch.setattr(library_mod, "project_schematic_files", lambda: [sch_file])
    monkeypatch.setattr(library_mod, "get_schematic_backend", lambda: FakeBackend())
    return library_mod._schematic_component_rows()


def _symbol(ref: str, value: str, datasheet: str) -> str:
    return (
        f'(symbol (lib_id "Lib:{value}") (property "Reference" "{ref}") '
        f'(property "Value" "{value}") (property "Footprint" "Pkg:SOT-23-5") '
        f'(property "Datasheet" "{datasheet}"))'
    )


def test_rows_carry_the_symbols_datasheet_and_drop_kicads_empty_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://www.diodes.com/assets/Datasheets/AP2112.pdf"
    text = "(kicad_sch " + _symbol("U2", "AP2112K", url) + _symbol("R1", "10k", "~") + ")"
    symbols = [
        {"reference": "U2", "value": "AP2112K", "footprint": "Pkg:SOT-23-5"},
        {"reference": "R1", "value": "10k", "footprint": "Pkg:SOT-23-5"},
    ]
    rows = {row["reference"]: row for row in _rows(tmp_path, monkeypatch, text, symbols)}

    assert rows["U2"]["datasheet"] == url
    assert rows["R1"]["datasheet"] == ""
    # The consolidated CSV writes every row key; the new field is one of its columns.
    assert set(rows["U2"]) <= set(BOM_FIELDNAMES) | {"reference"}


def test_grouped_rows_keep_the_first_datasheet_a_group_has() -> None:
    rows = [
        {"reference": "C1", "value": "10u", "footprint": "C_0805", "datasheet": ""},
        {"reference": "C2", "value": "10u", "footprint": "C_0805", "datasheet": "https://x/c.pdf"},
    ]
    (group,) = library_mod._group_bom_rows(rows)

    assert group["references"] == ["C1", "C2"]
    assert group["datasheet"] == "https://x/c.pdf"
